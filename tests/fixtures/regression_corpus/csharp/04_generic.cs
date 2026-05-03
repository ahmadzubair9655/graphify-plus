namespace Corpus;

public class Box<T> {
    private readonly System.Collections.Generic.List<T> items = new();
    public void Add(T v) => items.Add(v);
    public int Count => items.Count;
}
