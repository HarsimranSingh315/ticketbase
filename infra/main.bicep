// TicketBase on Azure - one resource group, sized for the free allowances.
//
//   Container Apps (Consumption)  web app, scales to zero  -> free monthly grant
//   Container Apps jobs           migrate (manual), sla-check (schedule) -> same grant
//   PostgreSQL Flexible Server    Burstable B1ms, 32 GB   -> free 12 months (free account)
//   Log Analytics                 logs for the environment -> first GBs/month free
//
// Deploy:  see docs/AZURE_DEPLOY.md. Every @secure() value is passed at deploy
// time and never stored in this repository.
//
// Known trade-off (fixed in the networking week, docs/AZURE_DEPLOY.md step 7):
// the database uses public access with the "allow Azure services" firewall rule,
// protected by TLS and a password. Private networking is the proper end state.

targetScope = 'resourceGroup'

@description('Azure region. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short prefix for resource names (lowercase letters/numbers).')
@maxLength(12)
param prefix string = 'ticketbase'

@description('Container image, e.g. ghcr.io/<github-user>/ticketbase:<commit-sha>')
param image string

@description('PostgreSQL admin user name.')
param postgresAdminLogin string = 'tbadmin'

@description('PostgreSQL admin password. Use URL-safe characters only (letters, digits, - and _).')
@secure()
param postgresAdminPassword string

@description('App SECRET_KEY, at least 32 characters.')
@secure()
param secretKey string

@description('App API_KEY, at least 32 characters.')
@secure()
param apiKey string

@description('Only for a PRIVATE image: GitHub user name for ghcr.io. Leave empty for a public image.')
param registryUsername string = ''

@description('Only for a PRIVATE image: a GitHub token with read:packages.')
@secure()
param registryPassword string = ''

@description('Tag every resource so cost and ownership are traceable (AZ-104: governance).')
param tags object = {
  project: 'ticketbase'
  environment: 'production'
}

var useRegistryAuth = !empty(registryUsername)
var dbName = 'ticketbase'
var pgServerName = '${prefix}-pg-${uniqueString(resourceGroup().id)}'

// --- Logs --------------------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// --- Database ----------------------------------------------------------------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgServerName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'   // the free-account SKU: pick anything else and you pay
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32      // free-account limit is 32 GB
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource pgDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: dbName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// 0.0.0.0 - 0.0.0.0 is Azure's special value for "allow Azure services".
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// --- Shared app configuration ----------------------------------------------------
var databaseUrl = 'postgresql://${postgresAdminLogin}:${postgresAdminPassword}@${pg.properties.fullyQualifiedDomainName}:5432/${dbName}?sslmode=require'

var secrets = concat([
  { name: 'database-url', value: databaseUrl }
  { name: 'secret-key', value: secretKey }
  { name: 'api-key', value: apiKey }
], useRegistryAuth ? [ { name: 'registry-password', value: registryPassword } ] : [])

var registries = useRegistryAuth ? [
  {
    server: 'ghcr.io'
    username: registryUsername
    passwordSecretRef: 'registry-password'
  }
] : []

var env = [
  { name: 'ENVIRONMENT', value: 'production' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'SECRET_KEY', secretRef: 'secret-key' }
  { name: 'API_KEY', secretRef: 'api-key' }
  { name: 'FORWARDED_ALLOW_IPS', value: '*' }       // only the Container Apps ingress reaches the app
  { name: 'RUN_MIGRATIONS_ON_START', value: 'false' } // migrations run as the separate 'migrate' job
  { name: 'PORT', value: '8000' }
]

// --- Container Apps environment ---------------------------------------------------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// --- Web app -------------------------------------------------------------------------
resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-web'
  location: location
  tags: tags
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: env
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/live', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/ready', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0   // scale to zero when idle = no compute charge (first request after idle is slower)
        maxReplicas: 1   // one replica: the login rate limiter keeps its counters in memory
      }
    }
  }
}

// --- Jobs --------------------------------------------------------------------------------
resource migrateJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-migrate'
  location: location
  tags: tags
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          command: [ 'alembic' ]
          args: [ 'upgrade', 'head' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: env
        }
      ]
    }
  }
}

resource slaJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-sla-check'
  location: location
  tags: tags
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 300
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: '*/15 * * * *'   // every 15 minutes, in UTC
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'sla-check'
          image: image
          command: [ 'python' ]
          args: [ 'sla_check.py', '--once' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: env
        }
      ]
    }
  }
}

output appUrl string = 'https://${web.properties.configuration.ingress.fqdn}'
output postgresServer string = pg.properties.fullyQualifiedDomainName
output migrateJobName string = migrateJob.name
