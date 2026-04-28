"""Small sample module for smoke testing."""

def add(a, b):
    """Return a + b."""
    return a + b

def divide(a, b):
    if b == 0:
        raise ValueError("zero")
    return a / b

class Calculator:
    """Stateful calculator."""
    def __init__(self):
        self.total = 0
    def accumulate(self, x):
        self.total += x
        return self.total
