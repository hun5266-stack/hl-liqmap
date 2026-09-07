"""Execute only publishing control flow with shell functions replacing all IO."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASH=str(Path('C:/Program Files/Git/bin/bash.exe')) if Path('C:/Program Files/Git/bin/bash.exe').exists() else shutil.which('bash')


@unittest.skipUnless(BASH,'bash unavailable')
class PublishingTests(unittest.TestCase):
    def run_shell(self,source):
        return subprocess.run([BASH,'--noprofile','--norc'],input=source,text=True,encoding='utf-8',
                              capture_output=True,timeout=15)

    def publish(self,failures):
        source=(ROOT/'vps/run.sh').read_text(encoding='utf-8')
        func=source[source.index('push_all() {'):source.index('\npush_all\n')]
        stubs=f'''
pushes=0
resets=0
git() {{
  case "$1" in
    push) pushes=$((pushes+1)); [ "$pushes" -gt {failures} ]; return $? ;;
    reset) resets=$((resets+1)); return 0 ;;
    diff) return 1 ;;
    *) return 0 ;;
  esac
}}
mktemp() {{ echo /fake-not-created; }}
cp() {{ :; }}
rm() {{ :; }}
sleep() {{ :; }}
date() {{ echo fixture; }}
'''
        result=self.run_shell(stubs+func+'\npush_all\nrc=$?\nprintf "RESULT %s %s %s" "$rc" "$pushes" "$resets"\n')
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_three_failures_leave_local_snapshot_for_report(self):
        self.assertIn('RESULT 1 3 2',self.publish(3))

    def test_retry_success(self):
        self.assertIn('RESULT 0 2 1',self.publish(1))

    def test_failed_publish_still_reports_with_warning(self):
        source=(ROOT/'vps/run.sh').read_text(encoding='utf-8')
        tail=source[source.index('\npush_all\n'):]
        stubs='''
BEFORE=old.csv.gz
AFTER=new.csv.gz
push_all() { return 1; }
python3() { printf 'REPORT %s\\n' "$*"; return 0; }
'''
        result=self.run_shell(stubs+tail)
        self.assertEqual(result.returncode,1)
        self.assertIn('--github-failed',result.stdout)
        self.assertIn('--snapshot new.csv.gz',result.stdout)
