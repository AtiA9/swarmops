output "instance_public_ip" {
  description = "Elastic IP of the SwarmOps EC2 host - feed this into ansible/inventory.ini"
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  value = aws_instance.app.id
}

output "security_group_id" {
  value = aws_security_group.app.id
}
