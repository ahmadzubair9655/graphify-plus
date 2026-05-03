package corpus;

public class Dog implements Speak {
    private final String name;
    public Dog(String name) { this.name = name; }
    public String speak() { return name + " barks"; }
}
