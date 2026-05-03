package corpus;

import java.util.ArrayList;
import java.util.List;

public class Box<T> {
    private final List<T> items = new ArrayList<>();
    public void add(T v) { items.add(v); }
    public int size() { return items.size(); }
}
