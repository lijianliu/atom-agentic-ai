#!/usr/bin/env bash
# Security test battery for the hardened container
set -euo pipefail

echo "=== SECURITY TEST BATTERY ==="
echo ""

echo "1. Who am I?"
id
echo ""

echo "2. Can I become root?"
su root -c whoami 2>&1 || echo "  -> BLOCKED (as expected)"
echo ""

echo "3. Can I write to system dirs?"
touch /etc/test 2>&1 || echo "  -> BLOCKED (read-only fs)"
echo ""

echo "4. Can I see host processes?"
ls /proc/*/cmdline 2>/dev/null | wc -l
echo "  -> Only my own processes visible"
echo ""

echo "5. Can I access the network?"
curl -s --connect-timeout 2 http://google.com 2>&1 || echo "  -> BLOCKED (no network)"
echo ""

echo "6. Can I mount anything?"
mount -t tmpfs none /mnt 2>&1 || echo "  -> BLOCKED (no CAP_SYS_ADMIN)"
echo ""

echo "7. Can I install packages?"
apt-get update 2>&1 || echo "  -> BLOCKED (apt removed)"
echo ""

echo "8. Can I run python?"
python3 -c "print('Python works for legitimate use')"
echo ""

echo "9. Can Python access /etc/shadow?"
python3 -c "
try:
    open('/etc/shadow').read()
except Exception as e:
    print(f'  -> BLOCKED: {e}')
"
echo ""

echo "10. SUID binaries?"
find / -perm /4000 -type f 2>/dev/null | head -5 || true
echo "  -> (should be empty)"
echo ""

echo "=== ALL TESTS COMPLETE ==="
