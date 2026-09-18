# Flow Modules

Exported flow modules from the [AWS Self-Service AI Agents Workshop](https://catalog.workshops.aws/self-service-ai-agents).

Import these into your Connect instance (**Flows → Modules → Create module → Import**) **before**
importing `config/Main Flow -- Option E.json` — the flow references the modules by name, so
importing the flow first fails on the missing reference.

| Module | Status | Notes |
|--------|--------|-------|
| `basic-setting-configurations_flow.json` | **Required** | Sets recording + analytics behaviour and creates the Q in Connect (Wisdom) session. The Option E flow invokes it directly. |
| `customer-profile-lookup_flow.json` | Optional | Customer profile lookup pattern. Harmless to import — every branch converges on the same next block — but it references a Lambda in the workshop account `055929692747`, so the lookup itself will not resolve in your account. |

After importing `basic-setting-configurations_flow.json`, open its **Create Wisdom session**
(a.k.a. *Create AI session*) block and point it at **your** Q in Connect assistant, then save and
publish. The exported module carries the workshop's assistant ARN, which does not exist in your
account.

`scripts/post_deploy.py` prints your assistant ARN, or fetch it with:

```bash
aws connect list-integration-associations \
  --instance-id "$CONNECT_INSTANCE_ID" \
  --integration-type WISDOM_ASSISTANT \
  --query 'IntegrationAssociationSummaryList[0].IntegrationArn' --output text
```
