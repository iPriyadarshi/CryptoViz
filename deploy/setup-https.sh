#!/usr/bin/env bash
#
# Serve an already-bootstrapped instance over HTTPS on a DuckDNS subdomain.
#
# Points the nginx site at the domain, obtains a Let's Encrypt certificate,
# redirects HTTP to HTTPS and verifies automatic renewal. The DuckDNS record
# must already point at this instance's Elastic IP.
#
# Safe to re-run: an existing valid certificate is kept.
#
# Usage:
#     sudo bash /opt/cryptoviz/deploy/setup-https.sh <name>.duckdns.org <email>

set -euo pipefail

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { echo "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This script must be run with sudo/root."
[[ $# -eq 2 ]] || die "Usage: sudo bash $0 <name>.duckdns.org <email>"

DOMAIN="${1,,}"
EMAIL="$2"

[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?\.duckdns\.org$ ]] \
    || die "Expected a domain like cryptoviz.duckdns.org, got: $DOMAIN"
[[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] \
    || die "Expected an email address for certificate notices, got: $EMAIL"
command -v apt-get >/dev/null 2>&1 \
    || die "This script supports Ubuntu/Debian only."

SITE="/etc/nginx/sites-available/cryptoviz"
[[ -f "$SITE" ]] || die "$SITE not found. Run deploy/bootstrap.sh first."

# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------
# Checked up front because a failed validation counts against Let's Encrypt's
# rate limits.
log "Checking that $DOMAIN points at this instance"
PUBLIC_IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}')"
[[ "$DNS_IP" == "$PUBLIC_IP" ]] \
    || die "$DOMAIN resolves to '${DNS_IP:-nothing}', but this instance is $PUBLIC_IP. Update the record at duckdns.org and retry."

# ---------------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------------
log "Installing certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq certbot python3-certbot-nginx

log "Setting the nginx server_name to $DOMAIN"
sed -i -E "s/^([[:space:]]*server_name)[[:space:]]+[^;]+;/\1 $DOMAIN;/" "$SITE"
nginx -t
systemctl reload nginx

log "Requesting the certificate"
certbot --nginx --non-interactive --agree-tos --keep-until-expiring \
        --redirect -m "$EMAIL" -d "$DOMAIN"

log "Verifying renewal"
certbot renew --dry-run

log "Checking the site over HTTPS"
curl -fsS --max-time 15 "https://$DOMAIN/healthz" >/dev/null \
    || die "https://$DOMAIN/healthz failed. Is port 443 open in the security group?"

log "HTTPS is live at https://$DOMAIN"
