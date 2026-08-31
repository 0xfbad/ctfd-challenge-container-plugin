from container_manager import ContainerException


def test_message_attribute():
    exc = ContainerException("something broke")
    assert exc.message == "something broke"
    assert str(exc) == "something broke"


def test_no_args_default():
    exc = ContainerException()
    assert exc.message == "unknown container exception"
    assert str(exc) == "unknown container exception"


def test_is_exception():
    exc = ContainerException("test")
    assert isinstance(exc, Exception)


def test_raisable():
    try:
        raise ContainerException("fail")
    except ContainerException as e:
        assert e.message == "fail"
