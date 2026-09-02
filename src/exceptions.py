class ContainerException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.message = str(args[0]) if args else "unknown container exception"

    def __str__(self) -> str:
        return self.message


# routes map this to 503 while a plain ContainerException is 500
class ContainerUnavailableException(ContainerException):
    pass
