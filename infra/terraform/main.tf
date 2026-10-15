# Low-cost GCP deployment (NOT applied — requires an authorized project and billing).
#
#   cd infra/terraform
#   terraform init
#   terraform plan  -var project_id=YOUR_PROJECT -var image=REGION-docker.pkg.dev/PROJECT/seacrime/app:TAG
#   terraform apply ...
#
# Components
#   * GCS bucket            persistent state (raw snapshots, processed tables, models, frozen forecasts, export)
#   * Artifact Registry     container images (built by .github/workflows/release.yml or `gcloud builds submit`)
#   * Cloud Run Job "cycle" hourly ingest/build/forecast/score/export (Cloud Scheduler trigger)
#   * Cloud Run Job "train" weekly production retraining
#   * Cloud Run service     read-only API + static dashboard, scales to zero
#   * Secret Manager        optional SOCRATA_APP_TOKEN / ANTHROPIC_API_KEY (values added out of band)
#   * Service accounts      least-privilege runtime identities
#
# Estimated cost: see docs/DEPLOYMENT.md (~$10-25 / month at hourly cadence).

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  labels = { app = "seacrime", purpose = "research" }
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com", "cloudscheduler.googleapis.com", "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com", "storage.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-seacrime-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels
  versioning { enabled = true } # protects frozen forecasts against accidental overwrite
  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["state/data/raw/fire_911/", "state/data/raw/spd_calls/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}

resource "google_artifact_registry_repository" "repo" {
  repository_id = "seacrime"
  format        = "DOCKER"
  location      = var.region
  labels        = local.labels
}

resource "google_service_account" "jobs" {
  account_id   = "seacrime-jobs"
  display_name = "seacrime scheduled jobs"
}

resource "google_service_account" "api" {
  account_id   = "seacrime-api"
  display_name = "seacrime read-only API"
}

resource "google_service_account" "scheduler" {
  account_id   = "seacrime-scheduler"
  display_name = "seacrime Cloud Scheduler invoker"
}

resource "google_storage_bucket_iam_member" "jobs_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_storage_bucket_iam_member" "api_ro" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret" "socrata" {
  secret_id = "seacrime-socrata-app-token"
  replication {
    auto {}
  }
  labels = local.labels
}

resource "google_secret_manager_secret_iam_member" "jobs_socrata" {
  secret_id = google_secret_manager_secret.socrata.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.jobs.email}"
}

locals {
  job_env = {
    SEACRIME_STATE_URI      = "gs://${google_storage_bucket.state.name}/state"
    SEACRIME_DATA_ROOT      = "/tmp/seacrime/data"
    SEACRIME_ARTIFACTS_ROOT = "/tmp/seacrime/artifacts"
    SEACRIME_EXPORT_DIR     = "/tmp/seacrime/export"
  }
}

resource "google_cloud_run_v2_job" "cycle" {
  name     = "seacrime-cycle"
  location = var.region
  labels   = local.labels
  template {
    task_count = 1
    template {
      service_account = google_service_account.jobs.email
      timeout         = "2400s"
      max_retries     = 1
      containers {
        image   = var.image
        command = ["sh", "-c", "python scripts/state_sync.py pull && python -m seacrime.prospective.cycle; rc=$?; python scripts/state_sync.py push; exit $rc"]
        resources {
          limits = { cpu = "4", memory = "16Gi" }
        }
        dynamic "env" {
          for_each = local.job_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "SOCRATA_APP_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.socrata.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_job" "train" {
  name     = "seacrime-train"
  location = var.region
  labels   = local.labels
  template {
    template {
      service_account = google_service_account.jobs.email
      timeout         = "10800s"
      containers {
        image   = var.image
        command = ["sh", "-c", "python scripts/state_sync.py pull && python -m seacrime.build && python -m seacrime.train && python scripts/state_sync.py push"]
        resources {
          limits = { cpu = "8", memory = "32Gi" }
        }
        dynamic "env" {
          for_each = local.job_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_job_iam_member" "invoke_cycle" {
  name     = google_cloud_run_v2_job.cycle.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_job_iam_member" "invoke_train" {
  name     = google_cloud_run_v2_job.train.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "hourly" {
  name      = "seacrime-hourly-cycle"
  schedule  = "10 * * * *"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.cycle.name}:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_cloud_scheduler_job" "weekly_train" {
  name      = "seacrime-weekly-train"
  schedule  = "30 3 * * 1"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.train.name}:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "seacrime-api"
  location = var.region
  labels   = local.labels
  ingress  = var.public ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_ONLY"
  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    volumes {
      name = "state"
      gcs {
        bucket    = google_storage_bucket.state.name
        read_only = true
      }
    }
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      env {
        name  = "SEACRIME_EXPORT_DIR"
        value = "/mnt/state/state/export"
      }
      volume_mounts {
        name       = "state"
        mount_path = "/mnt/state"
      }
      startup_probe {
        http_get { path = "/api/healthz" }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

# Public access is opt-in (var.public) and must be explicitly approved.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.public ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Alert when the hourly job fails (log-based metric + policy).
resource "google_logging_metric" "cycle_errors" {
  name   = "seacrime_cycle_errors"
  filter = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"seacrime-cycle\" AND severity>=ERROR"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "cycle_failures" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "seacrime hourly cycle errors"
  combiner     = "OR"
  conditions {
    display_name = "errors in seacrime-cycle"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.cycle_errors.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email[0].id]
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "seacrime alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}
