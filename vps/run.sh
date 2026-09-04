#!/bin/bash
# 하이퍼리퀴드 청산맵 수집 — 매시 정각 크론.
#
# 이 스크립트는 리베이스를 쓰지 않는다. 예전 판은 푸시가 거부되면
# git pull --rebase 로 얹으려 했는데, latest.json 과 holders.json 을
# VPS 와 Actions 가 매번 새로 쓰기 때문에 반드시 충돌한다.
# 한 번 충돌하면 unmerged 상태가 남아 그 뒤 모든 실행이 첫 줄에서 죽었다.
# 20시간을 조용히 놀았다. 그래서 두 가지를 바꿨다.
#   ① 매 실행을 origin 기준으로 새로 시작한다 (물려도 다음 시간에 저절로 풀린다)
#   ② 푸시가 거부되면 새 파일만 최신 위에 다시 얹는다 (멈출 수 있는 지점이 없다)
#
# 이 파일의 원본은 레포 vps/run.sh 다. 고칠 때 양쪽을 같이 고칠 것.
# 돌고 있는 bash 스크립트를 덮어쓰면 중간부터 잘못 읽으므로
# git reset 이 건드리지 못하도록 레포 밖(/root/run.sh)에 두고 돌린다.
cd /root/hl-liqmap || exit 1
exec >> /var/log/hl-liqmap.log 2>&1
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) start ==="

# 스캔이 한 시간을 넘겨 다음 크론과 겹치는 것을 막는다.
exec 9>/var/lock/hl-liqmap.lock
flock -n 9 || { echo "이전 실행이 아직 돈다. 건너뛴다"; exit 0; }

# 로컬에 아낄 상태가 없다. 스냅샷은 만들자마자 푸시하므로 항상 새로 시작한다.
git rebase --abort 2>/dev/null
git merge  --abort 2>/dev/null
git fetch -q origin || { echo "fetch 실패"; exit 1; }
git reset -q --hard origin/main
git clean -qfd

newest() { ls -1 data/*/*.csv.gz 2>/dev/null | tail -1; }
BEFORE=$(newest)

# --min-gap 은 스캔 시간을 빼고 잡아야 한다. 전수가 17~18분이라 스냅샷이
# HH:18 에 찍히고 다음 정시에는 42분 전으로 보인다. 45분이면 매번 걸러져
# 두 시간에 한 번밖에 못 돈다. 30분으로 두면 스캔이 30분까지 늘어져도 버틴다.
# 겹침 방지는 위의 flock 이 맡으므로 이 값은 Actions 와의 중복만 보면 된다.
# 인자를 넘기면 collect.py 로 그대로 전달된다 (수동 검증용: run.sh --force)
python3 collect.py --full --min-gap 30 "$@" || { echo "수집 실패"; exit 1; }

push_all() {
  git add -A data latest.json holders.json
  if git diff --staged --quiet; then echo "변경 없음"; return 0; fi
  local MSG="snapshot $(date -u +%Y-%m-%dT%H:%MZ) [vps]"
  git commit -q -m "$MSG"
  for i in 1 2 3; do
    if git push -q origin main; then echo "푸시 완료"; return 0; fi
    echo "푸시 거부 — 재정렬 $i"
    local T=$(mktemp -d)
    cp -a data latest.json holders.json "$T"/
    git fetch -q origin
    git reset -q --hard origin/main
    cp -a "$T"/data/. data/          # 스냅샷은 이름이 겹치지 않아 양쪽 다 남는다
    cp -a "$T"/latest.json "$T"/holders.json .
    rm -rf "$T"
    git add -A data latest.json holders.json
    git diff --staged --quiet || git commit -q -m "$MSG"
    sleep 5
  done
  echo "푸시 3회 실패"
  return 1
}

push_all
RC=$?

# 새 스냅샷이 실제로 생겼을 때만 디스코드로 보낸다.
# collect.py 가 --min-gap 으로 건너뛰면 보낼 것이 없다.
if [ "$BEFORE" != "$(newest)" ]; then
  # 전송이 실패해도 수집은 이미 끝났다. 종료 코드에 영향을 주지 않는다.
  python3 vps/report.py --out /tmp/liqmap.png || echo "리포트 실패"
else
  echo "새 스냅샷 없음 — 리포트 건너뜀"
fi

exit $RC
