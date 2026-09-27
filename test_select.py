import select, sys
if __name__ == '__main__':
    select.select([sys.stdin], [], [])
