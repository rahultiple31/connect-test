"""Custom Resource handler for MCP server registration with Amazon Connect.

Does two things CloudFormation cannot express natively:

1. Sets the AgentCore Gateway's JWT ``allowedAudience`` to the gateway's OWN
   ID. Connect sends the gateway ID as the ``aud`` claim, but a CFN resource
   cannot reference its own attributes (circular dependency), so the stack
   creates the gateway with a placeholder audience and this handler fixes it
   up post-create via ``UpdateGateway``.
2. Creates the AppIntegrations Application with ``ApplicationType=MCP_SERVER``
   (the ``AWS::AppIntegrations::Application`` CFN resource does not expose
   ``ApplicationType``).

Connect integration associations (APPLICATION + WISDOM_ASSISTANT) are NOT
created here — see scripts/post_deploy.py. The Lambda's role cannot call
``iam:PutRolePolicy`` on the Connect SLR, which ``CreateIntegrationAssociation``
requires internally.
"""

import json
import logging
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

appintegrations = boto3.client("appintegrations")
agentcore = boto3.client("bedrock-agentcore-control")


def _set_gateway_audience(gateway_id: str, discovery_url: str) -> None:
    """Point the gateway's CUSTOM_JWT authorizer at itself (audience = own ID).

    Read-modify-write: UpdateGateway requires name/roleArn/protocolType/
    authorizerType every call, so we echo the current values and change only
    the authorizer configuration. Idempotent.
    """
    gw = agentcore.get_gateway(gatewayIdentifier=gateway_id)
    current = (gw.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    if current.get("allowedAudience") == [gateway_id] and current.get("discoveryUrl") == discovery_url:
        logger.info("Gateway %s audience already correct — skipping", gateway_id)
        return

    kwargs = {
        "gatewayIdentifier": gateway_id,
        "name": gw["name"],
        "roleArn": gw["roleArn"],
        "protocolType": gw["protocolType"],
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedAudience": [gateway_id],
            }
        },
    }
    # Echo optional fields so UpdateGateway doesn't clear them.
    for k in ("description", "protocolConfiguration", "kmsKeyArn",
              "interceptorConfigurations", "exceptionLevel"):
        if gw.get(k):
            kwargs[k] = gw[k]

    agentcore.update_gateway(**kwargs)
    logger.info("Gateway %s allowedAudience set to its own ID", gateway_id)


def handler(event, context):
    """CloudFormation Custom Resource handler."""
    request_type = event["RequestType"]
    props = event["ResourceProperties"]

    gateway_url = props["GatewayUrl"]
    gateway_arn = props["GatewayArn"]
    gateway_id = props.get("GatewayId", "")
    app_name = props.get("ApplicationName", "ConnectAsyncMcpServer")
    app_namespace = props.get("ApplicationNamespace", "connect-async")
    discovery_url = props.get("DiscoveryUrl", "")

    response_data = {}
    physical_resource_id = event.get("PhysicalResourceId", "none")

    try:
        # Step 0 (Create + Update): fix the gateway's JWT audience.
        if request_type in ("Create", "Update") and gateway_id and discovery_url:
            _set_gateway_audience(gateway_id, discovery_url)

        if request_type == "Create":
            # Step 1: Create AppIntegrations Application with MCP_SERVER type
            logger.info("Creating AppIntegrations Application with MCP_SERVER type")
            app_response = appintegrations.create_application(
                Name=app_name,
                Namespace=app_namespace,
                ApplicationType="MCP_SERVER",
                Description=f"MCP server backed by AgentCore Gateway {gateway_arn}",
                ApplicationSourceConfig={
                    "ExternalUrlConfig": {
                        "AccessUrl": gateway_url,
                    }
                },
            )
            app_arn = app_response["Arn"]
            app_id = app_response["Id"]
            logger.info(f"Created Application: {app_arn}")

            physical_resource_id = json.dumps({
                "ApplicationArn": app_arn,
                "ApplicationId": app_id,
            })
            response_data = {
                "ApplicationArn": app_arn,
                "ApplicationId": app_id,
            }

        elif request_type == "Delete":
            if physical_resource_id and physical_resource_id != "none":
                ids = json.loads(physical_resource_id)

                # Delete AppIntegrations Application
                try:
                    logger.info(f"Deleting Application {ids.get('ApplicationArn')}")
                    appintegrations.delete_application(
                        Arn=ids["ApplicationArn"],
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete Application: {e}")

        elif request_type == "Update":
            # Delete old app, create new one
            if physical_resource_id and physical_resource_id != "none":
                try:
                    ids = json.loads(physical_resource_id)
                    appintegrations.delete_application(Arn=ids["ApplicationArn"])
                except Exception as e:
                    logger.warning(f"Cleanup of old application failed: {e}")

            # Create new app
            app_response = appintegrations.create_application(
                Name=app_name,
                Namespace=app_namespace,
                ApplicationType="MCP_SERVER",
                Description=f"MCP server backed by AgentCore Gateway {gateway_arn}",
                ApplicationSourceConfig={
                    "ExternalUrlConfig": {
                        "AccessUrl": gateway_url,
                    }
                },
            )
            app_arn = app_response["Arn"]
            app_id = app_response["Id"]

            physical_resource_id = json.dumps({
                "ApplicationArn": app_arn,
                "ApplicationId": app_id,
            })
            response_data = {
                "ApplicationArn": app_arn,
                "ApplicationId": app_id,
            }

        _send_response(event, context, "SUCCESS", response_data, physical_resource_id)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        _send_response(event, context, "FAILED", {"Error": str(e)}, physical_resource_id)


def _send_response(event, context, status, data, physical_resource_id):
    """Send response to CloudFormation."""
    body = json.dumps({
        "Status": status,
        "Reason": f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    })

    req = urllib.request.Request(
        event["ResponseURL"],
        data=body.encode("utf-8"),
        headers={"Content-Type": ""},
        method="PUT",
    )
    urllib.request.urlopen(req)
