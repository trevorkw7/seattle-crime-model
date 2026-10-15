output "state_bucket" {
  value = google_storage_bucket.state.name
}

output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}"
}
