import subprocess

API_KEY = "sk-thisisaverylonghardcodedsecretvalue1234567890"

def run_cmd(user_input):
    return subprocess.check_output(user_input, shell=True)

def evil(expr):
    return eval(expr)

def big_function(n):
    total = 0
    for i in range(n):
        for j in range(n):
            for k in range(n):
                total += i*j*k
    if n > 0:
        if n > 1:
            if n > 2:
                if n > 3:
                    if n > 4:
                        if n > 5:
                            if n > 6:
                                if n > 7:
                                    if n > 8:
                                        total += 1
    return total
