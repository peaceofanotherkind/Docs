"""
Copyright 2016-2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at

http://aws.amazon.com/apache2.0/

or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific language governing permissions and limitations under the License.
"""

from urllib.parse import urlencode
from urllib.request import urlopen, Request, HTTPError, URLError
import json


def send_response(event, context, response_status, reason=None, response_data={}):
    body = {
        "Status": response_status,
        "PhysicalResourceId": context.log_stream_name,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
    }

    print("Responding: {}".format(response_status))
    print("Responding data: {}".format(response_data))
    print("Responding reason: {}".format(reason))

    if reason:
        body["Reason"] = reason

    if response_data:
        body["Data"] = response_data

    body_bytes = json.dumps(body).encode("utf-8")

    if event.get("ResponseURL"):
        req = Request(event.get("ResponseURL"), data=body_bytes, headers={
            "Content-Length": len(body_bytes),
            "Content-Type": "",
        })
        
        req.get_method = lambda: "PUT"

        try:
            urlopen(req)
            if response_status == "FAILED":
                raise Exception("Failed executing HTTP request: " + json.dumps(body))

            return True
        except HTTPError as e:
            print("Failed executing HTTP request: {}".format(e.code))
            raise Exception("Failed executing HTTP request: " + str(e.code) + " for data: " + json.dumps(body))
            return False
        except URLError as e:
            print("Failed to reach the server: {}".format(e.reason))
            raise Exception("Failed executing HTTP request: " + str(e.reason) + " for data: " + json.dumps(body))
            return False
    else:
        print(body)