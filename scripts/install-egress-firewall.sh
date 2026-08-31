#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="${SERVICE_USER:-ytbdownload}"
RULE_FILE="/etc/nftables.d/ytbdownload-egress.nft"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo/root." >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Service user does not exist: $SERVICE_USER" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends nftables
install -d -m 0755 /etc/nftables.d
uid="$(id -u "$SERVICE_USER")"
DNS_RULES=""
while read -r resolver; do
  [[ -z "$resolver" ]] && continue
  if [[ "$resolver" == *:* ]]; then
    DNS_RULES+="    meta skuid $uid ip6 daddr $resolver udp dport 53 accept"$'\n'
    DNS_RULES+="    meta skuid $uid ip6 daddr $resolver tcp dport 53 accept"$'\n'
  elif [[ "$resolver" =~ ^[0-9.]+$ ]]; then
    DNS_RULES+="    meta skuid $uid ip daddr $resolver udp dport 53 accept"$'\n'
    DNS_RULES+="    meta skuid $uid ip daddr $resolver tcp dport 53 accept"$'\n'
  fi
done < <(awk '$1 == "nameserver" {print $2}' /etc/resolv.conf)

cat > "$RULE_FILE" <<EOF
# Defense-in-depth egress policy for Signal / yt-dlp Web.
# DNS and replies from the local app port are allowed; new connections to
# private, loopback, link-local, documentation and multicast ranges are blocked.
table inet signal_ytdlp {
  chain output {
    type filter hook output priority filter; policy accept;

${DNS_RULES}
    meta skuid $uid ip daddr 127.0.0.0/8 tcp sport 8000 accept
    meta skuid $uid ip6 daddr ::1 tcp sport 8000 accept

    meta skuid $uid ip daddr {
      0.0.0.0/8,
      10.0.0.0/8,
      100.64.0.0/10,
      127.0.0.0/8,
      169.254.0.0/16,
      172.16.0.0/12,
      192.0.0.0/24,
      192.0.2.0/24,
      192.168.0.0/16,
      198.18.0.0/15,
      198.51.100.0/24,
      203.0.113.0/24,
      224.0.0.0/4,
      240.0.0.0/4
    } reject with icmp type admin-prohibited

    meta skuid $uid ip6 daddr {
      ::/128,
      ::1/128,
      ::ffff:0.0.0.0/96,
      2001:db8::/32,
      fc00::/7,
      fe80::/10,
      ff00::/8
    } reject with icmpv6 type admin-prohibited
  }
}
EOF
chmod 0644 "$RULE_FILE"

# Replace only this project's table, leaving the host firewall untouched.
nft delete table inet signal_ytdlp 2>/dev/null || true
nft --check --file "$RULE_FILE"
nft --file "$RULE_FILE"

if ! grep -Fq 'include "/etc/nftables.d/*.nft"' /etc/nftables.conf; then
  printf '\ninclude "/etc/nftables.d/*.nft"\n' >> /etc/nftables.conf
fi
systemctl enable nftables.service >/dev/null

cat <<EOF
Installed egress filtering for UID $uid ($SERVICE_USER).
Test the app now:
  curl http://127.0.0.1:8000/api/v1/health/ready
To remove:
  sudo nft delete table inet signal_ytdlp
  sudo rm -f $RULE_FILE
EOF
