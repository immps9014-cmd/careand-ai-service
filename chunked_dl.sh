#!/bin/bash
cd /root/careand-ai-service/models/small-ct2 || exit 1
RESOLVE="https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin"
TOTAL=483546902
CHUNK=33554432
rm -f model.bin /tmp/ck
SIGNED=$(curl -s -o /dev/null -w '%{redirect_url}' "$RESOLVE")
off=0; idx=0
while [ "$off" -lt "$TOTAL" ]; do
  end=$((off+CHUNK-1)); [ "$end" -ge "$TOTAL" ] && end=$((TOTAL-1))
  want=$((end-off+1)); got=0
  for try in 1 2 3 4 5 6; do
    curl -s --max-time 120 -r ${off}-${end} -o /tmp/ck "$SIGNED"
    got=$(stat -c%s /tmp/ck 2>/dev/null || echo 0)
    [ "$got" = "$want" ] && break
    sleep 3
    SIGNED=$(curl -s -o /dev/null -w '%{redirect_url}' "$RESOLVE")
  done
  if [ "$got" != "$want" ]; then echo "CHUNK $idx FAILED off=$off got=$got want=$want"; exit 1; fi
  cat /tmp/ck >> model.bin
  cur=$(stat -c%s model.bin)
  echo "chunk $idx ok total=$cur/$TOTAL"
  off=$((end+1)); idx=$((idx+1))
done
rm -f /tmp/ck
FINAL=$(stat -c%s model.bin)
echo "FINAL size=$FINAL expected=$TOTAL"
[ "$FINAL" = "$TOTAL" ] && echo "MODELBIN_OK" || echo "MODELBIN_SIZE_MISMATCH"
