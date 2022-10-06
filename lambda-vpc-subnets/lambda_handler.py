"""
Copyright 2016-2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
http://aws.amazon.com/apache2.0/
or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific language governing permissions and limitations under the License.
"""


from .lambda_utils import send_response
import boto3
import json
import os
import uuid
import re

from boto3.dynamodb.conditions import Key, Attr
from netaddr.ip import IPNetwork
from netaddr.contrib.subnet_splitter import SubnetSplitter

def publish_metrics(table_name, request_region):
    #Metrics
    if os.environ.get('publishMetrics') != "yes":
        return

    try:
        cloudwatch_client = boto3.client('cloudwatch')
        ec2_client  = boto3.client('ec2')
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)

        ec2_response = ec2_client.describe_transit_gateways()

        for entry in ec2_response.get('TransitGateways'):
            transitGatewayId = entry.get('TransitGatewayId')

            for metric_type in ['Used', 'Free', 'Orphaned']:
                if metric_type == "Free":
                    table_response = table.scan(
                        FilterExpression=Key('region').eq(request_region) & Key('account_id').eq(0) & Key('tgw_id').eq(transitGatewayId),
                        ProjectionExpression='cidr_subnet'
                    )
                elif metric_type == "Used":
                    table_response = table.scan(
                        FilterExpression=Key('region').eq(request_region) & Key('account_id').gt(0) & Key('tgw_id').eq(transitGatewayId),
                        ProjectionExpression='cidr_subnet'
                    )
                elif metric_type == "Orphaned":
                    #Only when item was created 
                    table_response = table.scan(
                        FilterExpression=Key('region').eq(request_region) & Key('account_id').gt(0) & Key('tgw_id').eq(transitGatewayId) & Attr('stack_id').not_exists(),
                        ProjectionExpression='cidr_subnet,env'
                    )

                metrics = {}

                if table_response.get('Count') > 0:
                    scan_data = table_response['Items']
                    while 'LastEvaluatedKey' in table_response:
                        table_response = table.scan(ExclusiveStartKey=table_response['LastEvaluatedKey'])
                        scan_data.extend(table_response['Items'])

                    for entry in scan_data:
                        env = entry.get('env')
                        if not entry.get('env') or entry.get('cidr_subnet') == "":
                            env = "NONE"

                        if not metrics.get(env):
                            metrics[env] = {}

                        if not metrics.get(env).get(entry.get('cidr_subnet')):
                            metrics.get(env)[entry.get('cidr_subnet')] = 1
                        else:
                            metrics.get(env)[entry.get('cidr_subnet')] += 1

                    for metric_env, metric in metrics.items():
                        for metric_cidr, cidr_count in metric.items():
                            cloudwatch_response = cloudwatch_client.put_metric_data(
                                Namespace='Networks',
                                MetricData=[
                                    {
                                        'MetricName': 'Networks',
                                        'Dimensions': [
                                            {
                                                'Name': 'region',
                                                'Value': request_region
                                            },
                                            {
                                                'Name': 'tgw',
                                                'Value': transitGatewayId
                                            },
                                            {
                                                'Name': 'cidr_subnet',
                                                'Value': str(metric_cidr)
                                            },
                                            {
                                                'Name': 'type',
                                                'Value': metric_type
                                            },
                                            {
                                                'Name': 'enviroment',
                                                'Value': metric_env
                                            },

                                        ],
                                        'Value': cidr_count,
                                        'Unit': 'Count'
                                    },
                                ]
                            )
    except Exception as e:
        print(str(e))
        return False

def handler(event, context, responder=send_response):
    """
    Handle a CloudFormation custom resource event
    """
    properties = event.get("ResourceProperties", {})

    if event.get("RequestType") == "UpdateTable":

        request_region = properties.get("Region")
        
        if not request_region:
            return responder(event, context, "FAILED", reason="Missing parameter(s): Region")
        
    else:
        request_stack_id = event.get("StackId")
    
        missing = [param for param in ("AccountId", "Region", "VpcSize", "SubnetMask", "SubnetsCount", "TgwId") if param not in properties]
    
        if missing:
            return responder(event, context, "FAILED", reason="Missing parameter(s): {}".format(", ".join(missing)))
        
        request_account_id   = properties.get("AccountId")
        request_region       = properties.get("Region")
        request_vpc_size     = properties.get("VpcSize")
        request_subnet_size  = properties.get("SubnetMask")
        request_subnet_count = properties.get("SubnetsCount")
        request_tgwId        = properties.get("TgwId")
        request_env          = properties.get("Env") #Optional
        
        if not request_env:
            request_env = "NONE"
    
    # Dynamo variables
    dynamodb    = boto3.resource('dynamodb')
    ec2_client  = boto3.client('ec2')
    s3_client   = boto3.client('s3')
    table_name  = os.environ['tableName']
    bucket_name = os.environ['s3bucketName']
    

    #Update dynamo table based on current assigned route tables
    if event.get("RequestType") in ["UpdateTable", "Create", "Delete"]:
        try:
            response = ec2_client.describe_transit_gateways()
            table = dynamodb.Table(table_name)

            for entry in response.get('TransitGateways'):
                transitGatewayId = entry.get('TransitGatewayId')
                response_routes = ec2_client.describe_transit_gateway_route_tables(
                    Filters=[
                        {
                            'Name': 'transit-gateway-id',
                            'Values': [transitGatewayId]
                        },
                    ],
                )

                for route_entry in response_routes.get('TransitGatewayRouteTables'):
                    response = ec2_client.export_transit_gateway_routes(
                        TransitGatewayRouteTableId = route_entry.get('TransitGatewayRouteTableId'),
                        S3Bucket = bucket_name,
                    )
                    filename = re.sub('s3://' + bucket_name + "/", '', response.get('S3Location'))
                    data = s3_client.get_object(Bucket=bucket_name, Key=filename)
                    json_data = data['Body'].read().decode('utf-8')
                    json_content = json.loads(json_data)

                    for route in json_content.get('routes'):
                        if route.get('state') != 'blackhole' and route.get('destinationCidrBlock') != '0.0.0.0/0':
                            route_cidr = route.get('destinationCidrBlock').split('/')[1]
                            route_network = route.get('destinationCidrBlock').split('/')[0]
                            transitGatewayAttachmentId = route.get("transitGatewayAttachments")[0].get('transitGatewayAttachmentId')
                            describe_transit_gateway_attachments_response = ec2_client.describe_transit_gateway_attachments(
                                TransitGatewayAttachmentIds=[
                                    transitGatewayAttachmentId,
                                ]
                            )
                            route_owner = describe_transit_gateway_attachments_response.get('TransitGatewayAttachments')[0].get('ResourceOwnerId')

                            table_response = table.scan(
                                FilterExpression=Key('region').eq(request_region) & Key('cidr_subnet').eq(int(route_cidr)) & Key('cidr_prefix').eq(route_network) & Key('tgw_id').eq(transitGatewayId)
                            )
                            #Misssing subnet for this tgw
                            if table_response.get('Count') == 0:
                                response = table.put_item(
                                Item={
                                        'id': str(uuid.uuid4()),
                                        'region': request_region,
                                        'account_id': int(route_owner),
                                        'cidr_subnet': int(route_cidr),
                                        'cidr_prefix': route_network,
                                        'tgw_id': transitGatewayId,
                                        'env':  "NONE"
                                    }
                                )
                            elif table_response.get('Count') == 1:
                                #Existing but not in use cidr_prefix
                                if table_response.get('Items')[0].get('account_id') == 0:
                                    update_response = table.update_item(
                                        Key={
                                            'id': table_response.get('Items')[0].get('id'),
                                            'region': table_response.get('Items')[0].get('region')
                                        },
                                        UpdateExpression="set account_id = :a REMOVE stack_id",
                                        ExpressionAttributeValues={
                                            ':a': int(route_owner)
                                        },
                                        ReturnValues="UPDATED_NEW"
                                    )
                                #elif table_response['Count'] == route_owner:
                                    #nothing to change
                                #elif table_response['Count'] != route_owner:
                                    #log + notification, there is conflict
                            #else:
                                #to many records conflict
        except Exception as e:
            return responder(event, context, "FAILED", reason=str(e))

        if event.get("RequestType") == "UpdateTable":
            publish_metrics(table_name, request_region)

    # Delete 
    if event.get("RequestType") == "Delete":
        try:
            table = dynamodb.Table(table_name)
            
            response = table.scan(
                FilterExpression=Key('region').eq(request_region) & Key('account_id').eq(int(request_account_id)) & Key('stack_id').eq(request_stack_id)
            )

            if response.get('Count') == 0:
                return responder(event, context, "FAILED", reason="No subnets found for specified criteria: " + json.dumps(properties) )
                
            else:
                subnet_to_delete = response.get('Items')[0]
                update_response = table.update_item( 
                    Key={
                        'id': subnet_to_delete.get('id'),
                        'region': subnet_to_delete.get('region')
                    },
                    UpdateExpression="set account_id = :a REMOVE stack_id",
                    ExpressionAttributeValues={
                        ':a': int(0)
                    },
                    ReturnValues="UPDATED_NEW"
                )
                publish_metrics(table_name, request_region)

                return responder(event, context, "SUCCESS")
                
        except Exception as e:
            return responder(event, context, "FAILED", reason=str(e))
            

    elif event.get("RequestType") == "Create":
        # Check the imputs are valid
        if int(request_subnet_size) < 16 and int(request_subnet_size) >= 28:
            return responder(event, context, "FAILED", reason="An invalid subnet size was specified for subnets: " + request_subnet_size )
        if int(request_vpc_size) < 16 and int(request_vpc_size) >= 28:
            return responder(event, context, "FAILED", reason="An invalid subnet size was specified for VPC: " + request_vpc_size )
        if int(request_subnet_size) < int(request_vpc_size):
            return responder(event, context, "FAILED", reason="Requested subnet size was bigger then VPC size" )

        vpc_cidr = ""
        subnet_cidrs = []
        
        try:
            table = dynamodb.Table(table_name)
            response = table.scan(
                FilterExpression=Key('region').eq(request_region) & Key('account_id').eq(0) & Key('cidr_subnet').eq(int(request_vpc_size)) & Key('tgw_id').eq(request_tgwId) & Key('env').eq(request_env)
            )

            if response.get('Count') == 0:
                return responder(event, context, "FAILED", reason="No unused subnets found for specified criteria: " + json.dumps(properties) )
            else:
                subnet_to_take = response.get('Items')[0]
                vpc_cidr = subnet_to_take.get('cidr_prefix')+'/'+ str(subnet_to_take.get('cidr_subnet'))
                update_response = table.update_item( 
                    Key={
                        'id': subnet_to_take.get('id'),
                        'region': subnet_to_take.get('region')
                    },
                    UpdateExpression="set account_id = :a, stack_id = :b ",
                    ExpressionAttributeValues={
                        ':a': int(request_account_id),
                        ':b': request_stack_id
                    },
                    ReturnValues="UPDATED_NEW"
                )
        #Nothing to rollback in this case
        except Exception as e:
            return responder(event, context, "FAILED", reason=str(e))


        try:
            splitter = SubnetSplitter(vpc_cidr)
            available_subnets = list(splitter.extract_subnet(int(request_subnet_size), count=int(request_subnet_count)))
            if len(available_subnets) == 0:
                raise Exception(str(e))

        except:
            update_response = table.update_item( 
                Key={
                    'id': subnet_to_take['id'],
                    'region': subnet_to_take['region']
                },
                    UpdateExpression="set account_id = :a REMOVE stack_id",
                    ExpressionAttributeValues={
                        ':a': int(0)
                    },
                ReturnValues="UPDATED_NEW"
            )
            return responder(event, context, "FAILED", reason="Requested subnets are imposible to create in specified VPC subnet")
    
        response_data={}
        response_data["VpcCidr"] = vpc_cidr
        
        for i, cidr_block in enumerate(available_subnets):
            response_data["Subnet{}".format(i + 1)] = str(cidr_block)
            
        publish_metrics(table_name, request_region)

        return responder(event, context, "SUCCESS", response_data=response_data)