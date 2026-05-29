variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "server_name" {
  type    = string
  default = "voicehook-v4"
}

variable "server_type" {
  type    = string
  default = "cx23"
}

variable "location" {
  type    = string
  default = "nbg1"
}

variable "image" {
  type    = string
  default = "ubuntu-24.04"
}

variable "ssh_key_name" {
  type    = string
  default = "voicehook-v4"
}

variable "domain" {
  type    = string
  default = "voicehook.ai"
}

# Caddy site hostnames + LK public WSS URL. For prod leave defaults. For a
# staging/test box without DNS use sslip.io patterns (e.g.
# caddy_site_main="178-105-177-133.sslip.io",
# caddy_site_rtc="rtc-178-105-177-133.sslip.io",
# livekit_public_url="wss://rtc-178-105-177-133.sslip.io").
variable "caddy_site_main" {
  type    = string
  default = "voicehook.ai"
}

variable "caddy_site_rtc" {
  type    = string
  default = "rtc.voicehook.ai"
}

variable "livekit_public_url" {
  description = "Public WSS URL the browser uses to reach LiveKit signaling (Caddy-fronted)."
  type        = string
  default     = "wss://rtc.voicehook.ai"
}

# --- secrets (rendered into the box .env / livekit.yaml by cloud-init) -----

variable "invite_secret" {
  description = "HMAC key for /api/token invite codes (server-side only)."
  type        = string
  sensitive   = true
}

variable "livekit_api_key" {
  type      = string
  sensitive = true
}

variable "livekit_api_secret" {
  type      = string
  sensitive = true
}

variable "deepgram_api_key" {
  type      = string
  sensitive = true
}

variable "google_api_key" {
  type      = string
  sensitive = true
}

variable "gcp_sa_json" {
  description = "Full GCP service-account JSON string (for Cloud TTS)."
  type        = string
  sensitive   = true
}

variable "git_repo" {
  type    = string
  default = "voicehook-ai/voicehook-v4"
}

variable "git_ref" {
  type    = string
  default = "main"
}

variable "github_deploy_token" {
  description = "Optional fine-grained read-only PAT for cloud-init's git clone."
  type        = string
  sensitive   = true
  default     = ""
}
