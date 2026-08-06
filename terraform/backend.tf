# Remote state (not local .tfstate) so it survives a laptop dying and so `terraform
# apply` from CI or a teammate's machine sees the same state. DynamoDB provides the
# lock table so two concurrent applies can't stomp on each other.
#
# The bucket/table must exist BEFORE `terraform init` - that's normally done once via
# a tiny separate "bootstrap" apply (or the two aws CLI commands below), never through
# this same config (a backend can't provision the thing it depends on to store its
# own state). Not run in this repo since it costs real AWS spend and needs real
# credentials - see README.md "What was/wasn't run against real cloud".
#
#   aws s3api create-bucket --bucket swarmops-tfstate-<your-unique-suffix> \
#     --region il-central-1 --create-bucket-configuration LocationConstraint=il-central-1
#   aws dynamodb create-table --table-name swarmops-tf-lock \
#     --attribute-definitions AttributeName=LockID,AttributeType=S \
#     --key-schema AttributeName=LockID,KeyType=HASH \
#     --billing-mode PAY_PER_REQUEST --region il-central-1

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "swarmops-tfstate-CHANGE-ME" # must be globally-unique; set before init
    key            = "swarmops/terraform.tfstate"
    region         = "il-central-1"
    dynamodb_table = "swarmops-tf-lock"
    encrypt        = true
  }
}
