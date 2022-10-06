#!/bin/bash

echo "Make sure your AWS_PROFILE points to the master account, and press enter"
read x

set -u

# List account names:
aws --output table organizations list-accounts --query "Accounts[].[Name]"

set -x

# Enable RAM in Organizations:
aws ram enable-sharing-with-aws-organization 

# Enable Organization Access:
aws servicecatalog enable-aws-organizations-access 

echo "All OK"
