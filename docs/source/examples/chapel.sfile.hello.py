from pych.extern import Chapel

@Chapel(sfile="chapel.hellolib.chpl")
def hello_world():
    return None

if __name__ == "__main__":
    hello_world()
