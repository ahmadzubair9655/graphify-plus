module Greetable
  def greet
    "hello #{name}"
  end
end

class Person
  include Greetable
  attr_reader :name
  def initialize(name)
    @name = name
  end
end
