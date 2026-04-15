class ContainerException(Exception):
    def __init__(self, *args):
        super().__init__(*args)
        self.message = args[0] if args else "unknown container exception"

    def __str__(self):
        return self.message
