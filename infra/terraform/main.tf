data "hcloud_ssh_key" "default" {
  name = var.ssh_key_name
}

resource "hcloud_server" "agent" {
  name        = var.server_name
  server_type = var.server_type
  location    = var.location
  image       = var.image
  ssh_keys    = [data.hcloud_ssh_key.default.id]

  user_data = templatefile("${path.module}/cloud-init.yaml", {
    domain              = var.domain
    invite_secret       = var.invite_secret
    livekit_api_key     = var.livekit_api_key
    livekit_api_secret  = var.livekit_api_secret
    deepgram_api_key    = var.deepgram_api_key
    google_api_key      = var.google_api_key
    gcp_sa_json_b64     = base64encode(var.gcp_sa_json)
    git_repo            = var.git_repo
    git_ref             = var.git_ref
    github_deploy_token = var.github_deploy_token
  })

  labels = { app = "voicehook", version = "v4" }
}

output "ipv4" {
  value = hcloud_server.agent.ipv4_address
}

output "ssh" {
  value = "ssh root@${hcloud_server.agent.ipv4_address}"
}
