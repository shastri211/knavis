from app.policy import sufficient
def test_gate(): assert sufficient([.5],.35); assert not sufficient([.1],.35)
