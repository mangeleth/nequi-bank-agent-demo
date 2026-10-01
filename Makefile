# Load settings from .env (git-ignored; see .env.example) and pass them to every command.
include .env
export

.PHONY: venv az-check providers rg-create aks-create aks-rbac aks-creds aks-verify aks-stop aks-start acr-create acr-attach acr-login kv-create kv-addon aoai-create aoai-check test guard-clean build push deploy smoke release

## Create a local virtualenv with the script dependencies (uv: no system python3-venv needed)
venv:
	uv venv --allow-existing --python 3.12 .venv
	uv pip install --python .venv/bin/python -q -r requirements-dev.txt

## Run the unit tests
test:
	.venv/bin/python -m pytest -q

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

# ---------------------------------------------------------------------------------------------
# Delivery (ADR-0004): make release SERVICE=core-systems
# ---------------------------------------------------------------------------------------------
SERVICE ?= core-systems
SERVICE_DIR = services/$(subst -,_,$(SERVICE))
IMAGE_TAG := $(shell git rev-parse --short HEAD)
IMAGE = $(ACR_NAME).azurecr.io/$(SERVICE):$(IMAGE_TAG)

## Refuse to release uncommitted code: every image tag must map to an exact commit
guard-clean:
	@git diff --quiet HEAD -- && test -z "$$(git ls-files --others --exclude-standard)" \
		|| (echo "Uncommitted changes: commit first, image tags must map to a commit (ADR-0004)"; exit 1)

## Build the service image from the repo root (so it can include shared/)
build:
	docker build -f $(SERVICE_DIR)/Dockerfile -t $(IMAGE) .

push: acr-login
	docker push $(IMAGE)

## Apply the namespace and the service manifests with the image pinned to this commit
deploy:
	kubectl apply -f k8s/namespace.yaml
	cat k8s/$(SERVICE)/*.yaml | sed 's|__IMAGE__|$(IMAGE)|' | kubectl apply -f -
	kubectl rollout status deployment/$(SERVICE) -n $(K8S_NAMESPACE) --timeout=180s

## Call the service from inside the cluster via its ClusterIP DNS name
smoke:
	scripts/smoke.sh $(K8S_NAMESPACE) http://$(SERVICE)/healthz

## Full pipeline: clean tree -> tests -> build -> push -> deploy -> smoke
release: guard-clean test build push deploy smoke
