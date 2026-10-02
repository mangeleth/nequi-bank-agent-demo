# ADR-0024: The demo UI is published on the internet, open to anyone, with no sign-in

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M7.5

## Context
Until now the demo UI had no public address (ADR-0023): it was opened with a port-forward on a
machine with access to the cluster. The demo should be reachable from any browser by a link,
for example for an interviewer, and the project owner decided that no sign-in is needed.

## Decision
- **One public address, for the UI only:** a separate Kubernetes Service, `demo-ui-public`, of
  type LoadBalancer, with an Azure DNS name:
  `http://nequi-disputes-demo.eastus2.cloudapp.azure.com` (`DEMO_UI_DNS_LABEL`).
- **Nothing else is exposed.** The intake API, the agents, Core Systems, PostgreSQL, Redis, and
  the queues keep their ClusterIP Services.
- **Kept apart from the UI's manifests** (`k8s/public/`), so a normal deploy never publishes it.
  `make ui-publish` adds the address; `make ui-unpublish` removes it, and the UI keeps running
  inside the cluster.
- **No sign-in, by decision.** The data is synthetic (ADR-0003).
- **The model spend a visitor can cause is bounded without a new limiter.** The deduplication
  gate (ADR-0015) allows one dispute per customer and transfer: the UI offers 3 customers and
  11 transfers, so at most 11 investigations (about $0.15) between two `make demo-reset`s.
  Submitting again shows the existing dispute at no cost. The page says so.

## Consequences
- + The demo opens from any browser, with nothing to install.
- + The rest of the system stays exactly as reachable as before: not at all from outside.
- - **Anyone with the link can use it**, as any of the synthetic customers.
- - **Plain HTTP:** traffic is not encrypted and browsers mark the page "Not secure". Acceptable
  for synthetic data; HTTPS needs a certificate (see the production delta).
- - Visitors share one demo state: after a transfer has been disputed, everyone sees that
  result until `make demo-reset`.
- - A public IP and a load-balancer rule cost a little while published (a few dollars a month).

## Production delta
A customer-facing app sits behind the bank's identity provider, never open; HTTPS with a managed
certificate (an ingress controller with cert-manager, or Azure Application Gateway) and HSTS; a
web application firewall and per-client rate limits in front of it; the intake API published
through an API gateway rather than the UI holding a signing key.
