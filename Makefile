# Load settings from .env (git-ignored; see .env.example) and pass them to every command.
include .env
export

.PHONY: test-db test-servicebus venv az-check providers rg-create aks-create aks-rbac aks-creds aks-verify aks-stop aks-start acr-create acr-attach acr-login kv-create kv-addon aoai-create aoai-check demo-token internal-key-local run-core run-fraud run-ledger run-supervisor wi-create kv-grant jwt-publish smoke-fraud smoke-ledger smoke-triage eval eval-cluster redis-image postgres-image postgres-password signing-key signing-grant signing-key-publish servicebus-create sb-grant demo-reset test guard-clean validate build push deploy smoke release

## Create a local virtualenv with the script dependencies (uv: no system python3-venv needed)
venv:
	uv venv --allow-existing --python 3.12 .venv
	uv pip install --python .venv/bin/python -q -r requirements-dev.txt

## Run the unit tests
test:
	.venv/bin/python -m pytest -q

## Run the tests, including the PostgreSQL store, against a throwaway PostgreSQL in Docker
test-db:
	@docker rm -f disputes-test-db >/dev/null 2>&1 || true
	@docker run -d --rm --name disputes-test-db -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine >/dev/null
	@for i in $$(seq 30); do docker exec disputes-test-db pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done; sleep 1
	@TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/python -m pytest -q; \
		status=$$?; docker rm -f disputes-test-db >/dev/null; exit $$status

## Test the Service Bus adapter against the real service, on a separate test queue. Uses your
## own `az login`; the first run grants you access to that queue (it can take a minute to apply).
test-servicebus:
	@az servicebus queue create -g $(AKS_RESOURCE_GROUP) --namespace-name $(SERVICEBUS_NAMESPACE) -n disputes-test \
		--lock-duration PT5M --max-delivery-count 2 -o none
	@az role assignment create -o none --assignee $$(az ad signed-in-user show --query id -o tsv) \
		--role "Azure Service Bus Data Owner" \
		--scope $$(az servicebus queue show -g $(AKS_RESOURCE_GROUP) --namespace-name $(SERVICEBUS_NAMESPACE) -n disputes-test --query id -o tsv)
	TEST_SERVICEBUS_NAMESPACE=$(SERVICEBUS_NAMESPACE) .venv/bin/python -m pytest -q tests/test_servicebus_queue.py

## Show the logged-in Azure account and active subscription
az-check:
	az account set --subscription $(AZURE_SUBSCRIPTION_ID)
	az account show -o table

## Enable the Azure services this project needs (one-time per subscription).
## --wait blocks until each provider reaches "Registered" (can take a few minutes).
providers:
	az provider register --namespace Microsoft.Compute --wait
	az provider register --namespace Microsoft.ContainerService --wait
	az provider register --namespace Microsoft.CognitiveServices --wait
	az provider register --namespace Microsoft.OperationalInsights --wait
	az provider register --namespace Microsoft.ContainerRegistry --wait
	az provider register --namespace Microsoft.KeyVault --wait
	az provider register --namespace Microsoft.ServiceBus --wait

## Create the resource group (az group create is naturally idempotent)
rg-create:
	az group create --name $(AKS_RESOURCE_GROUP) --location $(AZURE_LOCATION) -o table

## Create the AKS cluster per ADR-0002. Skips creation if the cluster already exists.
aks-create: rg-create
	@if az aks show -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) -o none 2>/dev/null; then \
		echo "Cluster $(AKS_CLUSTER_NAME) already exists - skipping create."; \
	else \
		az aks create \
			--resource-group $(AKS_RESOURCE_GROUP) \
			--name $(AKS_CLUSTER_NAME) \
			--location $(AZURE_LOCATION) \
			--tier free \
			--node-count $(AKS_NODE_COUNT) \
			--node-vm-size $(AKS_NODE_VM_SIZE) \
			--enable-oidc-issuer \
			--enable-workload-identity \
			--enable-aad \
			--enable-azure-rbac \
			--disable-local-accounts \
			--generate-ssh-keys \
			-o table; \
	fi

## Grant the signed-in user Kubernetes admin rights on this cluster (Azure RBAC for Kubernetes).
## Needed because local accounts are disabled: without it, kubectl returns "Forbidden".
aks-rbac:
	az role assignment create \
		--assignee $$(az ad signed-in-user show --query id -o tsv) \
		--role "Azure Kubernetes Service RBAC Cluster Admin" \
		--scope $$(az aks show -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) --query id -o tsv) \
		-o table

## Write kubeconfig and switch it to use the az CLI login (kubelogin)
aks-creds:
	az aks get-credentials -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) --overwrite-existing
	kubelogin convert-kubeconfig -l azurecli

## Verify identity, ARM state, and Kubernetes access (Step 1b)
aks-verify:
	.venv/bin/python scripts/azure_setup.py

## Stop/start the cluster to save money between sessions
aks-stop:
	az aks stop -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME)

aks-start:
	az aks start -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME)

## Create the container registry (Basic SKU, admin user disabled; ADR-0004). Skips if it exists.
acr-create:
	@if az acr show -n $(ACR_NAME) -o none 2>/dev/null; then \
		echo "Registry $(ACR_NAME) already exists - skipping create."; \
	else \
		az acr create -g $(AKS_RESOURCE_GROUP) -n $(ACR_NAME) -l $(AZURE_LOCATION) \
			--sku Basic --admin-enabled false -o table; \
	fi

## Let the cluster pull images: grants the kubelet identity AcrPull (no imagePullSecrets).
## (`az aks check-acr` is not used: it needs local accounts, which ADR-0002 disables.)
acr-attach:
	az aks update -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) --attach-acr $(ACR_NAME) -o none
	az role assignment list -o table --query "[].roleDefinitionName" \
		--assignee $$(az aks show -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) --query identityProfile.kubeletidentity.objectId -o tsv) \
		--scope $$(az acr show -n $(ACR_NAME) --query id -o tsv)

## Log Docker in to the registry with your Entra ID token (short-lived, no password)
acr-login:
	az acr login -n $(ACR_NAME)

## Create the Key Vault in RBAC mode (ADR-0005) and let the signed-in user manage secrets.
## Even the subscription Owner cannot read secrets in RBAC mode until granted a data-plane role.
kv-create:
	@if az keyvault show -n $(KEYVAULT_NAME) -o none 2>/dev/null; then \
		echo "Key Vault $(KEYVAULT_NAME) already exists - skipping create."; \
	else \
		az keyvault create -g $(AKS_RESOURCE_GROUP) -n $(KEYVAULT_NAME) -l $(AZURE_LOCATION) \
			--enable-rbac-authorization true -o table; \
	fi
	az role assignment create -o none \
		--assignee $$(az ad signed-in-user show --query id -o tsv) \
		--role "Key Vault Secrets Officer" \
		--scope $$(az keyvault show -n $(KEYVAULT_NAME) --query id -o tsv)

## Install the Secrets Store CSI driver + Azure Key Vault provider on the cluster
kv-addon:
	az aks enable-addons -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) \
		--addons azure-keyvault-secrets-provider -o none
	kubectl get pods -n kube-system -l 'app in (secrets-store-csi-driver,secrets-store-provider-azure)'

## Create Azure OpenAI (ADR-0010): Entra ID auth only (API keys disabled), one model deployment
## pinned to an exact version (no automatic upgrades), and permission for the signed-in user
## to call it. Safe to run twice.
aoai-create:
	@if az cognitiveservices account show -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) -o none 2>/dev/null; then \
		echo "Azure OpenAI $(AOAI_NAME) already exists - skipping create."; \
	else \
		az cognitiveservices account create -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) -l $(AZURE_LOCATION) \
			--kind OpenAI --sku S0 --custom-domain $(AOAI_NAME) --yes -o none; \
	fi
	az resource update -o none --set properties.disableLocalAuth=true \
		--ids $$(az cognitiveservices account show -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) --query id -o tsv)
	az cognitiveservices account deployment create -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) \
		--deployment-name $(AOAI_DEPLOYMENT) --model-name $(AOAI_MODEL) --model-format OpenAI \
		--model-version $(AOAI_MODEL_VERSION) --sku-name Standard --sku-capacity $(AOAI_CAPACITY_K_TPM) -o none
	az resource update -o none --set properties.versionUpgradeOption=NoAutoUpgrade \
		--ids $$(az cognitiveservices account deployment show -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) \
			--deployment-name $(AOAI_DEPLOYMENT) --query id -o tsv)
	az role assignment create -o none \
		--assignee $$(az ad signed-in-user show --query id -o tsv) \
		--role "Cognitive Services OpenAI User" \
		--scope $$(az cognitiveservices account show -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) --query id -o tsv)

## Call the model with temperature 0 using your Entra ID login (no API key)
aoai-check:
	.venv/bin/python scripts/aoai_check.py

## Give a service's pods their own Azure identity (Workload Identity, ADR-0001): a managed
## identity, permission to call Azure OpenAI, and trust in the service's Kubernetes ServiceAccount.
## Safe to run twice. Usage: make wi-create SERVICE=fraud-agent
## A service that never calls a model gets no model access: make wi-create SERVICE=postgres WI_OPENAI=false
WI_OPENAI ?= true
wi-create:
	az identity create -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) -l $(AZURE_LOCATION) -o none
	@if [ "$(WI_OPENAI)" = "true" ]; then \
		az role assignment create -o none \
			--assignee-object-id $$(az identity show -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) --query principalId -o tsv) \
			--assignee-principal-type ServicePrincipal \
			--role "Cognitive Services OpenAI User" \
			--scope $$(az cognitiveservices account show -g $(AKS_RESOURCE_GROUP) -n $(AOAI_NAME) --query id -o tsv); \
	else echo "id-$(SERVICE): no Azure OpenAI access (WI_OPENAI=false)"; fi
	az identity federated-credential create -o none \
		--name aks-$(K8S_NAMESPACE)-$(SERVICE) --identity-name id-$(SERVICE) -g $(AKS_RESOURCE_GROUP) \
		--issuer $$(az aks show -g $(AKS_RESOURCE_GROUP) -n $(AKS_CLUSTER_NAME) --query oidcIssuerProfile.issuerUrl -o tsv) \
		--subject system:serviceaccount:$(K8S_NAMESPACE):$(SERVICE) \
		--audiences api://AzureADTokenExchange

## Let a service's identity read specific Key Vault secrets: read-only, and only those secrets.
## Usage: make kv-grant SERVICE=supervisor SECRETS="langfuse-public-key langfuse-secret-key"
kv-grant:
	@for secret in $(SECRETS); do \
		echo "granting id-$(SERVICE) read access to $$secret"; \
		az role assignment create -o none \
			--assignee-object-id $$(az identity show -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) --query principalId -o tsv) \
			--assignee-principal-type ServicePrincipal \
			--role "Key Vault Secrets User" \
			--scope $$(az keyvault show -n $(KEYVAULT_NAME) --query id -o tsv)/secrets/$$secret; \
	done

## Copy the Redis image into our registry, so the cluster pulls only from ACR. Safe to run twice.
redis-image:
	az acr import --name $(ACR_NAME) --source docker.io/library/redis:7.4-alpine --image redis:7.4-alpine --force -o none

## Copy the PostgreSQL image into our registry. Safe to run twice.
postgres-image:
	az acr import --name $(ACR_NAME) --source docker.io/library/postgres:17-alpine --image postgres:17-alpine --force -o none

## Generate the database password straight into Key Vault. It is never printed or written to
## disk. Skips if the secret exists (PostgreSQL only reads it when the database is first created).
postgres-password:
	@if az keyvault secret show --vault-name $(KEYVAULT_NAME) -n postgres-password -o none 2>/dev/null; then \
		echo "postgres-password already exists in $(KEYVAULT_NAME) - not changed."; \
	else \
		az keyvault secret set --vault-name $(KEYVAULT_NAME) -n postgres-password \
			--value "$$(openssl rand -base64 36 | tr -d '/+=\n')" -o none \
		&& echo "postgres-password created in $(KEYVAULT_NAME)."; \
	fi

SIGNING_KEY = supervisor-signing-key

## Create the key the supervisor signs its own tokens with (ADR-0017). The private half is
## generated inside Key Vault and cannot be read out. Grant its use with `make signing-grant`.
## Skips creation if the key exists (a new version would invalidate the published public key).
signing-key:
	az role assignment create -o none --role "Key Vault Crypto Officer" \
		--assignee $$(az ad signed-in-user show --query id -o tsv) \
		--scope $$(az keyvault show -n $(KEYVAULT_NAME) --query id -o tsv)
	@if az keyvault key show --vault-name $(KEYVAULT_NAME) -n $(SIGNING_KEY) -o none 2>/dev/null; then \
		echo "$(SIGNING_KEY) already exists in $(KEYVAULT_NAME) - not changed."; \
	else \
		for i in 1 2 3 4 5 6; do \
			az keyvault key create --vault-name $(KEYVAULT_NAME) -n $(SIGNING_KEY) --kty RSA --size 2048 \
				--ops sign verify -o none 2>/dev/null && echo "$(SIGNING_KEY) created." && break; \
			echo "waiting for the Crypto Officer role to take effect..."; sleep 20; \
		done; \
	fi

## Allow a service's identity to sign with that key. Only the service that runs triages needs it.
## Usage: make signing-grant SERVICE=triage-worker
signing-grant:
	az role assignment create -o none --role "Key Vault Crypto User" \
		--assignee-object-id $$(az identity show -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) --query principalId -o tsv) \
		--assignee-principal-type ServicePrincipal \
		--scope $$(az keyvault show -n $(KEYVAULT_NAME) --query id -o tsv)/keys/$(SIGNING_KEY)

## Publish the PUBLIC half of that key to the cluster, so the agents can check the signatures
signing-key-publish:
	@mkdir -p .local && rm -f .local/supervisor-signing-public.pem
	az keyvault key download --vault-name $(KEYVAULT_NAME) -n $(SIGNING_KEY) --encoding PEM \
		--file .local/supervisor-signing-public.pem
	kubectl create configmap internal-jwt-public-key -n $(K8S_NAMESPACE) \
		--from-file=internal-jwt-public.pem=.local/supervisor-signing-public.pem --dry-run=client -o yaml | kubectl apply -f -

## Create the dispute queue (ADR-0018): a Service Bus namespace that accepts Entra ID logins only
## (no connection strings), and a queue with a 5-minute lock and at most 2 deliveries, after
## which a message moves to the dead-letter queue. Safe to run twice.
servicebus-create:
	@if az servicebus namespace show -g $(AKS_RESOURCE_GROUP) -n $(SERVICEBUS_NAMESPACE) -o none 2>/dev/null; then \
		echo "Service Bus $(SERVICEBUS_NAMESPACE) already exists - skipping create."; \
	else \
		az servicebus namespace create -g $(AKS_RESOURCE_GROUP) -n $(SERVICEBUS_NAMESPACE) -l $(AZURE_LOCATION) \
			--sku Basic --disable-local-auth true -o none; \
	fi
	az servicebus queue create -g $(AKS_RESOURCE_GROUP) --namespace-name $(SERVICEBUS_NAMESPACE) -n $(SERVICEBUS_QUEUE) \
		--lock-duration PT5M --max-delivery-count 2 --enable-dead-lettering-on-message-expiration true -o none

## Allow an identity to send to OR receive from the queue, never both.
## Usage: make sb-grant SERVICE=supervisor SB_ROLE=Sender | make sb-grant SERVICE=triage-worker SB_ROLE=Receiver
sb-grant:
	az role assignment create -o none \
		--assignee-object-id $$(az identity show -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) --query principalId -o tsv) \
		--assignee-principal-type ServicePrincipal \
		--role "Azure Service Bus Data $(SB_ROLE)" \
		--scope $$(az servicebus queue show -g $(AKS_RESOURCE_GROUP) --namespace-name $(SERVICEBUS_NAMESPACE) -n $(SERVICEBUS_QUEUE) --query id -o tsv)

## Forget every dispute: gate keys in Redis and records in PostgreSQL (demo and evaluation only)
demo-reset:
	kubectl exec -n $(K8S_NAMESPACE) deploy/redis -- redis-cli FLUSHDB
	kubectl exec -n $(K8S_NAMESPACE) postgres-0 -- psql -q -U disputes -d disputes -c "TRUNCATE disputes CASCADE"

## Publish the demo identity provider's PUBLIC key to the cluster (it verifies tokens; not a secret)
jwt-publish:
	@test -f .local/jwt-public.pem || .venv/bin/python scripts/demo_token.py user-1001 >/dev/null
	kubectl create configmap jwt-public-key -n $(K8S_NAMESPACE) \
		--from-file=jwt-public.pem=.local/jwt-public.pem --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------------------------
# Local development: run each in its own terminal, then call the agent with a demo token
# ---------------------------------------------------------------------------------------------
USER_ID ?= user-1001
TX ?= TX-20261001000001

## Print a 15-minute login token for a synthetic customer (creates .local/ keys on first use)
demo-token:
	@.venv/bin/python scripts/demo_token.py $(USER_ID)

## Key pair for the supervisor's own tokens, for LOCAL runs only (the cluster's key lives in Key Vault)
internal-key-local:
	@mkdir -p .local
	@test -f .local/internal-jwt-private.pem || ( \
		openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .local/internal-jwt-private.pem && \
		chmod 600 .local/internal-jwt-private.pem && \
		openssl pkey -in .local/internal-jwt-private.pem -pubout -out .local/internal-jwt-public.pem && \
		echo "created .local/internal-jwt-*.pem" )

run-core:
	.venv/bin/uvicorn services.core_systems.app:app --port 8001

# The Langfuse keys are read from Key Vault for the process being started; they are never
# written to disk.
LANGFUSE_KEYS = \
	LANGFUSE_PUBLIC_KEY=$$(az keyvault secret show --vault-name $(KEYVAULT_NAME) -n langfuse-public-key --query value -o tsv) \
	LANGFUSE_SECRET_KEY=$$(az keyvault secret show --vault-name $(KEYVAULT_NAME) -n langfuse-secret-key --query value -o tsv)

run-fraud:
	$(LANGFUSE_KEYS) .venv/bin/uvicorn services.fraud_agent.main:create_app --factory --port 8002

run-ledger:
	$(LANGFUSE_KEYS) .venv/bin/uvicorn services.ledger_agent.main:create_app --factory --port 8003

run-supervisor:
	$(LANGFUSE_KEYS) .venv/bin/uvicorn services.supervisor.main:create_app --factory --port 8004

# ---------------------------------------------------------------------------------------------
# Evaluation against the real model (ADR-0014). A full run costs about $0.20 in model usage.
# ---------------------------------------------------------------------------------------------
SUPERVISOR_URL ?= http://127.0.0.1:8004
EVAL_LABEL ?= local

## Run evals/scenarios.json against a running supervisor; fails if any scenario fails
eval:
	$(LANGFUSE_KEYS) .venv/bin/python -m evals.run --url $(SUPERVISOR_URL) --label $(EVAL_LABEL)

## Evaluate the system deployed on AKS, through a temporary port-forward to the supervisor
eval-cluster:
	@mkdir -p .local
	@$(MAKE) --no-print-directory demo-reset
	@kubectl port-forward -n $(K8S_NAMESPACE) svc/supervisor 18004:80 >/dev/null 2>&1 & echo $$! > .local/port-forward.pid
	@sleep 4
	@$(MAKE) --no-print-directory eval SUPERVISOR_URL=http://127.0.0.1:18004 EVAL_LABEL=aks; status=$$?; \
		kill $$(cat .local/port-forward.pid) 2>/dev/null; rm -f .local/port-forward.pid; exit $$status

# ---------------------------------------------------------------------------------------------
# Delivery (ADR-0004): make release SERVICE=core-systems
# ---------------------------------------------------------------------------------------------
SERVICE ?= core-systems
WORKLOAD ?= deployment# statefulset for postgres
SERVICE_DIR = services/$(subst -,_,$(SERVICE))
IMAGE_TAG := $(shell git rev-parse --short HEAD)
IMAGE = $(ACR_NAME).azurecr.io/$(SERVICE):$(IMAGE_TAG)

## Refuse to release uncommitted code: every image tag must map to an exact commit
guard-clean:
	@git diff --quiet HEAD -- && test -z "$$(git ls-files --others --exclude-standard)" \
		|| (echo "Uncommitted changes: commit first, image tags must map to a commit (ADR-0004)"; exit 1)

## Check that every Kubernetes manifest is well-formed after its placeholders are filled.
## Catches a broken file before anything is built or applied.
validate:
	@for f in k8s/namespace.yaml k8s/*/*.yaml; do \
		IMAGE=x IMAGE_TAG=x WI_CLIENT_ID=x AZURE_TENANT_ID=x envsubst < $$f | kubectl apply --dry-run=client -f - >/dev/null \
			|| { echo "INVALID MANIFEST: $$f"; exit 1; }; \
	done; echo "manifests valid"

## Build the service image from the repo root (so it can include shared/)
build:
	docker build -f $(SERVICE_DIR)/Dockerfile -t $(IMAGE) .

push: acr-login
	docker push $(IMAGE)

## Apply the namespace and the service manifests. ${...} placeholders are filled from .env, the
## image is pinned to this commit, and WI_CLIENT_ID is the service's managed identity (if any).
## Config and secret mounts are applied BEFORE the workload: a pod reads them once, when it
## starts, so a pod created first would start with the old ones.
RENDER = IMAGE=$(IMAGE) IMAGE_TAG=$(IMAGE_TAG) WI_CLIENT_ID=$$(az identity show -g $(AKS_RESOURCE_GROUP) -n id-$(SERVICE) --query clientId -o tsv 2>/dev/null) \
	AZURE_TENANT_ID=$$(az account show --query tenantId -o tsv) \
	envsubst '$$IMAGE $$IMAGE_TAG $$SERVICEBUS_NAMESPACE $$SERVICEBUS_QUEUE $$ACR_NAME $$WI_CLIENT_ID $$AZURE_TENANT_ID $$KEYVAULT_NAME $$AOAI_NAME $$AOAI_DEPLOYMENT $$AOAI_API_VERSION $$JWT_ISSUER $$JWT_AUDIENCE $$LANGFUSE_BASE_URL'
WORKLOAD_FILES = k8s/$(SERVICE)/deployment.yaml k8s/$(SERVICE)/statefulset.yaml

deploy:
	kubectl apply -f k8s/namespace.yaml
	cat $(filter-out $(WORKLOAD_FILES),$(wildcard k8s/$(SERVICE)/*.yaml)) | $(RENDER) | kubectl apply -f -
	cat $(filter $(WORKLOAD_FILES),$(wildcard k8s/$(SERVICE)/*.yaml)) | $(RENDER) | kubectl apply -f -
	kubectl rollout status $(WORKLOAD)/$(SERVICE) -n $(K8S_NAMESPACE) --timeout=240s

## Call the service from inside the cluster via its ClusterIP DNS name
smoke:
	scripts/smoke.sh $(K8S_NAMESPACE) http://$(SERVICE)/healthz

## End-to-end check of the deployed Fraud Agent: log in as a synthetic customer and dispute
## their failed transfer, from inside the cluster
smoke-fraud:
	scripts/smoke.sh $(K8S_NAMESPACE) http://fraud-agent/v1/fraud/assessments \
		-H "Authorization: Bearer $$(.venv/bin/python scripts/demo_token.py $(USER_ID))" \
		-H "Content-Type: application/json" \
		-d '{"transaction_id":"TX-20261001000001","reason":"failed_transfer","claimed_amount":"50000.00"}'

## End-to-end check of the deployed Ledger Agent (reaches Core Banking over MCP)
smoke-ledger:
	scripts/smoke.sh $(K8S_NAMESPACE) http://ledger-agent/v1/ledger/reconciliations \
		-H "Authorization: Bearer $$(.venv/bin/python scripts/demo_token.py $(USER_ID))" \
		-H "Content-Type: application/json" \
		-d '{"transaction_id":"TX-20261001000001","reason":"failed_transfer","claimed_amount":"50000.00"}'

## Submit a dispute to the deployed supervisor (answers 202; follow it with GET /v1/disputes/<id>)
smoke-triage:
	scripts/smoke.sh $(K8S_NAMESPACE) http://supervisor/v1/disputes \
		-H "Authorization: Bearer $$(.venv/bin/python scripts/demo_token.py $(USER_ID))" \
		-H "Content-Type: application/json" \
		-d '{"transaction_id":"$(TX)","reason":"failed_transfer","claimed_amount":"50000.00"}'

## Full pipeline: clean tree -> tests -> manifests valid -> build -> push -> deploy -> smoke
release: guard-clean test validate build push deploy smoke
