from lorarepeater.timesync import parse_timedatectl, summarise

SHOW = """Timezone=Europe/Amsterdam
LocalRTC=no
CanNTP=yes
NTP=yes
NTPSynchronized=yes
TimeUSec=Sun 2026-10-25 16:34:26 CET
"""

SHOW_TIMESYNC = """LinkNTPServers=
SystemNTPServers=
FallbackNTPServers=0.debian.pool.ntp.org 1.debian.pool.ntp.org
ServerName=0.debian.pool.ntp.org
ServerAddress=194.109.6.2
RootDistanceMaxUSec=5s
PollIntervalUSec=34min 8s
NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, RootDelay=1.2ms }
Frequency=-1234567
"""


def test_summarise_synced():
    s = summarise(parse_timedatectl(SHOW), parse_timedatectl(SHOW_TIMESYNC))
    assert s == {"synchronized": True, "ntp_enabled": True, "timezone": "Europe/Amsterdam",
                 "server": "0.debian.pool.ntp.org", "server_address": "194.109.6.2",
                 "stratum": 2, "poll_interval": "34min 8s"}


def test_summarise_without_timesyncd():
    s = summarise(parse_timedatectl("NTP=no\nNTPSynchronized=no\n"), {})
    assert s["synchronized"] is False and s["server"] is None and s["stratum"] is None
