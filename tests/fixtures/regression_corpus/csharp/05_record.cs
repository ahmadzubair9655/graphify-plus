namespace Corpus;

public record Point(double X, double Y) {
    public double Magnitude() => System.Math.Sqrt(X * X + Y * Y);
}
