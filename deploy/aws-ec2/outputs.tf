output "dashboard_url" {
  description = "Public HTTP URL for the read-only dashboard"
  value       = "http://${aws_instance.srotaai.public_dns}/"
}

output "instance_id" {
  value = aws_instance.srotaai.id
}

output "region" {
  value = var.aws_region
}

output "cloudwatch_log_group" {
  description = "Container logs are retained for seven days"
  value       = aws_cloudwatch_log_group.app.name
}

output "vpc_id" {
  value = aws_vpc.demo.id
}
