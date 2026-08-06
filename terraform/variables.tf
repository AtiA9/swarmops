variable "aws_region" {
  description = "AWS region to deploy into. il-central-1 (Israel/Tel Aviv) by default."
  type        = string
  default     = "il-central-1"
}

variable "project_name" {
  type    = string
  default = "swarmops"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  type    = string
  default = "10.0.1.0/24"
}

variable "instance_type" {
  type    = string
  default = "t3.medium"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair to attach for SSH access. Create one first with `aws ec2 create-key-pair`."
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to reach port 22. No default on purpose - the whole point of stage 5 is 'not 0.0.0.0/0 on everything'; set this to your own IP/32 (e.g. via `curl ifconfig.me`)."
  type        = string
}
