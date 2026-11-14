# -*- coding: utf-8 -*-
from services import health_bridge as H

class State:
    def __init__(self, status, since=0):
        self.health = {"status": status, "since": since}

class Amo:
    def __init__(self, st): self.state = st

def test_bridge_reads_amo_state():
    assert H.get_sick_status(Amo(State("肠胃炎"))) == "肠胃炎"
    assert H.is_sick(Amo(State("肠胃炎"))) is True
    assert H.is_sick(Amo(State("疲惫"))) is False
    assert H.get_severity(Amo(State("发烧"))) == 3
    assert H.get_severity(Amo(State("健康"))) == 0

def test_bridge_none_safe():
    assert H.is_sick(None) is False and H.get_severity(None) == 0
