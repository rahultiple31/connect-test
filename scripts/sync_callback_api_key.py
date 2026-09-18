"""Post-deploy: read the API Gateway API key and write it to Secrets Manager.

This bridges the gap where CDK creates the API key and the secret separately.
Run once after each fresh deploy.
"""
import boto3

apigw = boto3.client("apigateway", region_name="us-east-1")
sm = boto3.client("secretsmanager", region_name="us-east-1")

# Find the API key
keys = apigw.get_api_keys(nameQuery="ConnectAsyncCallbackApiKey", includeValues=True)
if not keys["items"]:
    print("ERROR: API key 'ConnectAsyncCallbackApiKey' not found")
    exit(1)

api_key_value = keys["items"][0]["value"]
print(f"API Key ID: {keys['items'][0]['id']}")
print(f"API Key value: {api_key_value[:8]}...{api_key_value[-4:]}")

# Write to Secrets Manager
sm.put_secret_value(
    SecretId="connect-async/CALLBACK_API_KEY",
    SecretString=api_key_value,
)
print("Written to connect-async/CALLBACK_API_KEY ✅")
