# 수집 서버 (Vultr Tokyo)

매시 정각 크론이 `/root/run.sh` 를 돌린다. **`run.sh` 는 이 폴더의 사본이 원본이다.**

돌고 있는 bash 스크립트를 덮어쓰면 중간부터 잘못 읽으므로, `git reset --hard` 가
건드리지 못하도록 실제 실행본은 레포 밖(`/root/run.sh`)에 둔다. 고칠 때 둘 다 고칠 것.

## 서버가 날아갔을 때 복구

```
apt update && apt install -y git python3
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
# 출력된 공개키를 레포 Settings > Deploy keys 에 write 권한으로 등록
git clone git@github.com:hun5266-stack/hl-liqmap.git /root/hl-liqmap
cp /root/hl-liqmap/vps/run.sh /root/run.sh && chmod +x /root/run.sh
crontab - <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 * * * * /root/run.sh
CRON
```

로그는 `/var/log/hl-liqmap.log`.

## 살아 있는지 확인

커밋 작성자를 본다. VPS 는 `hl-liqmap vps`, Actions 는 `hl-liqmap bot` 이다.
`[vps]` 표식이 몇 시간째 없으면 서버가 죽은 것이고, 그동안은 Actions 가 성기게 받아준다.

```
gh api repos/hun5266-stack/hl-liqmap/commits --jq '.[] | "\(.commit.committer.date) \(.commit.committer.name)"'
```
