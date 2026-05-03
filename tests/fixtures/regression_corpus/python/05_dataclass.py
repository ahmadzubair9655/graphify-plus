from dataclasses import dataclass


@dataclass
class Point:
    x: float
    y: float

    def magnitude(self):
        return (self.x**2 + self.y**2) ** 0.5
