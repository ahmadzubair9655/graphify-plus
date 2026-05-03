namespace Corpus;

public class Dog : ISpeak {
    private readonly string name;
    public Dog(string name) { this.name = name; }
    public string Speak() => $"{name} barks";
}
