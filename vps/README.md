# 수집 서버 (Vultr Tokyo)

## 검토 수정본 적용

이 절차는 서버에서 실행한다. 로컬 코드 수정만으로 배포되지는 않는다. 기존 수집 실행이 끝난 뒤 코드를 반영하고, 레포 밖의 실행본도 교체한다.

```bash
cd /root/hl-liqmap
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
cp vps/run.sh /root/run.sh
chmod +x /root/run.sh
cp market.py /root/hl-liqmap-market.py
cp vps/hl-liqmap-mark.service /etc/systemd/system/hl-liqmap-mark.service
systemctl daemon-reload
systemctl enable --now hl-liqmap-mark.service
```

마크가격 기록기는 지갑 조회와 독립적으로 5초마다 BTC 마크가격을 기록한다. 저장 위치는 `/root/.local/share/hl-liqmap/marks`이며 매시간 Git 초기화의 영향을 받지 않는다. 수집기가 새 스냅샷을 저장할 때 최근 가격 표본과 캔들도 함께 `data/`에 압축 보관한다. API 제한이 관측되면 가격 주기와 재시도 로그를 함께 점검한다.

```bash
systemctl status hl-liqmap-mark.service
journalctl -u hl-liqmap-mark.service -n 30 --no-pager
```

서비스를 아직 설치하지 않았거나 가격 기록이 비면 청산가 도달 여부는 미확인으로 나온다. 일반 거래 캔들만으로 마크가격 도달을 확정하지 않는다. 마크 기록기는 실패한 조회를 가격으로 채우지 않는다.

GitHub 업로드가 최종 실패해도 VPS는 서버 자료로 Discord 브리핑을 시도하고 첫 줄에 저장 실패 경고를 붙인다. 합의에 따라 별도 재업로드 보관소는 두지 않는다. 다음 실행의 Git 초기화에서 해당 원본이 빠질 수 있다. Discord 자체 전송도 실패하면 서버 로그에만 남는다. GitHub Actions 백업은 원래처럼 Discord를 전송하지 않는다.

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
