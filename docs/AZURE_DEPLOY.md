# Deploying TicketBase to Azure

Everything runs from **Azure Cloud Shell** (the `>_` icon at the top of the
Azure portal) - no installs on your computer. Each step notes the **AZ-104
skill** it exercises.

> Never paste passwords, keys or connection strings into chat, screenshots or
> commits. The commands below keep them inside your Cloud Shell session.

## 0. Before anything: cost guardrails  *(AZ-104: governance / cost management)*
1. Create the Azure free account. To keep the **12-month free services**
   (including the PostgreSQL server), upgrade to **pay-as-you-go within 30
   days** - you're still only charged beyond the free amounts.
2. Portal -> **Cost Management** -> **Budgets** -> **Add**: amount **$5**/month,
   alerts at 50 %, 80 %, 100 % to your email. Do this *first*.
3. What this deployment uses, and why it should cost ~$0:
   | Resource | Free allowance |
   |---|---|
   | Container Apps (web + 2 jobs) | monthly free grant; web scales to zero when idle |
   | PostgreSQL Flexible Server **B1ms, 32 GB** | free 12 months on the free account |
   | Log Analytics | first few GB of logs per month |
   Choosing a bigger database SKU, turning on high availability, or leaving
   lab resources (Bastion, VPN gateways, VMs) running **will** cost money.

## 1. Publish the image (GitHub)
1. Push your code to `main`. The **CI** workflow runs the tests; when it
   passes, **Publish image** builds and pushes
   `ghcr.io/harsimransingh315/ticketbase:<commit-sha>`.
2. GitHub -> your profile -> **Packages** -> `ticketbase` -> **Package
   settings** -> **Change visibility** -> **Public**. (A private image also
   works - pass `registryUsername` and `registryPassword` in step 4.)
3. Copy the full image name with the commit SHA from the workflow's last step.

## 2. Open Cloud Shell and prepare  *(AZ-104: resource providers)*
Choose **Bash**. If asked about storage, **"No storage account required"** is fine.
```bash
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.DBforPostgreSQL
git clone https://github.com/HarsimranSingh315/ticketbase.git && cd ticketbase
```

## 3. Resource group with tags  *(AZ-104: resource groups, tags)*
```bash
RG=rg-ticketbase
LOC=canadacentral     # if a later step says the region/SKU isn't available on your subscription, use eastus
az group create --name $RG --location $LOC --tags project=ticketbase environment=production
```

## 4. Generate secrets and deploy  *(AZ-104: ARM/Bicep deployments)*
```bash
# Guaranteed to meet Azure's password rules (upper, lower, digit, symbol) and to be URL-safe.
PG_PASS="Tb1-$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
IMAGE=ghcr.io/harsimransingh315/ticketbase:PASTE_COMMIT_SHA

az deployment group create --resource-group $RG --template-file infra/main.bicep \
  --parameters image=$IMAGE postgresAdminPassword=$PG_PASS secretKey=$SECRET_KEY apiKey=$API_KEY
```
The database takes ~5-10 minutes. Before closing Cloud Shell, **save the three
secrets in a password manager** - these variables vanish with the session.

## 5. Run the database migrations  *(AZ-104: Container Apps jobs)*
```bash
az containerapp job start --name ticketbase-migrate --resource-group $RG
az containerapp job execution list --name ticketbase-migrate --resource-group $RG -o table   # wait for Succeeded
```
Until this succeeds, the web app refuses to start with one clear line naming
both schema versions - that's intended.

## 6. Create your admin account, then remove the bootstrap credentials
```bash
ADMIN_PW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"; echo "Admin password: $ADMIN_PW"
az containerapp secret set --name ticketbase-web --resource-group $RG --secrets bootstrap-pw=$ADMIN_PW
az containerapp update --name ticketbase-web --resource-group $RG \
  --set-env-vars BOOTSTRAP_ADMIN_EMAIL=YOUR_EMAIL BOOTSTRAP_ADMIN_PASSWORD=secretref:bootstrap-pw
```
Log in once at the app URL (from `az containerapp show -n ticketbase-web -g $RG --query properties.configuration.ingress.fqdn -o tsv`),
change your password under **Account**, then remove the bootstrap settings:
```bash
az containerapp update --name ticketbase-web --resource-group $RG --remove-env-vars BOOTSTRAP_ADMIN_EMAIL BOOTSTRAP_ADMIN_PASSWORD
az containerapp secret remove --name ticketbase-web --resource-group $RG --secret-names bootstrap-pw
```

## 7. Verify
```bash
curl -s https://$(az containerapp show -n ticketbase-web -g $RG --query properties.configuration.ingress.fqdn -o tsv)/ready
```
Expect `"status":"ready"` and `"schema":"ok"`. The first request after the app
has been idle takes a few extra seconds: it's scaling up from zero.

**Keep Render running until this passes. Then** delete the Render web service
and database.

## Troubleshooting
| Symptom | Where to look / fix |
|---|---|
| App won't start | `az containerapp logs show -n ticketbase-web -g $RG --follow` |
| Log says "Database schema is at ... but this build needs ..." | Re-run step 5 |
| "Refusing to start" | A secret is missing or shorter than 32 characters |
| Migration job failed | `az containerapp job logs show -n ticketbase-migrate -g $RG` |

## Updating to a new version
Push to `main` -> wait for **Publish image** -> then:
```bash
az containerapp update -n ticketbase-web -g $RG --image ghcr.io/harsimransingh315/ticketbase:NEW_SHA
az containerapp job update -n ticketbase-migrate -g $RG --image ghcr.io/harsimransingh315/ticketbase:NEW_SHA
az containerapp job update -n ticketbase-sla-check -g $RG --image ghcr.io/harsimransingh315/ticketbase:NEW_SHA
az containerapp job start -n ticketbase-migrate -g $RG
```

## Deleting everything
```bash
az group delete --name rg-ticketbase --yes
```
One command removes every resource in the group - a key reason to keep a
project in its own resource group.
