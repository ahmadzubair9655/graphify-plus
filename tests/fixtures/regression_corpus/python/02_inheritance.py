import abc


class Shape(abc.ABC):
    @abc.abstractmethod
    def area(self): ...


class Circle(Shape):
    def __init__(self, r):
        self.r = r

    def area(self):
        return 3.14 * self.r * self.r


__all__ = ["Shape", "Circle"]
