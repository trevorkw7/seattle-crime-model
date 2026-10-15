variable "project_id" {
  type        = string
  description = "GCP project id (must have billing enabled; nothing is created until you apply)"
}

variable "region" {
  type    = string
  default = "us-west1" # Oregon, close to Seattle
}

variable "image" {
  type        = string
  description = "Container image, e.g. us-west1-docker.pkg.dev/PROJECT/seacrime/app:TAG"
}

variable "public" {
  type        = bool
  default     = false
  description = "Expose the dashboard/API publicly. Keep false until explicitly approved."
}

variable "alert_email" {
  type    = string
  default = ""
}
