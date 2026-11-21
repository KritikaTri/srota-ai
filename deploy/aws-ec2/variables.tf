variable "aws_region" {
  description = "AWS region for the public demo"
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "EC2 size; small is recommended because this instance builds the container image"
  type        = string
  default     = "t3.small"
}

variable "repo_url" {
  description = "Public Git repository cloned by EC2 during bootstrap"
  type        = string
  default     = "https://github.com/KritikaTri/srota-ai.git"
}

variable "repo_branch" {
  description = "Branch to deploy"
  type        = string
  default     = "main"
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated demo VPC"
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR block for the single public subnet"
  type        = string
  default     = "10.42.1.0/24"
}
