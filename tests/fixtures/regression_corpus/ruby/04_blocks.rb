class Repeater
  def repeat(n)
    n.times { |i| yield i }
  end
end
