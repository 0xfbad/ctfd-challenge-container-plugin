class ContainerException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.message = str(args[0]) if args else "unknown container exception"

    def __str__(self) -> str:
        return self.message


# raised when the container infrastructure is unavailable (no connected docker
# contexts). routes map this to 503, distinct from generic ContainerException 500
class ContainerUnavailableException(ContainerException):
    pass
