class Logger
  @@instance = nil
  def self.instance
    @@instance ||= new
  end

  def log(msg)
    puts msg
  end
end
