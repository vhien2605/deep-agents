CXX      ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Isrc
COVFLAGS  = --coverage -O0 -g

SRC   := $(wildcard src/*.cpp)
TESTS := $(wildcard tests/*.cpp)

.PHONY: all test coverage clean

all: test_runner

test_runner: $(SRC) $(TESTS)
	$(CXX) $(CXXFLAGS) $(SRC) $(TESTS) -o $@

test: test_runner
	./test_runner

coverage: clean
	$(CXX) $(CXXFLAGS) $(COVFLAGS) $(SRC) $(TESTS) -o test_runner_cov
	./test_runner_cov
	lcov --capture --directory . --output-file coverage.info \
	     --ignore-errors inconsistent,unsupported,unused,empty
	lcov --remove coverage.info '/usr/*' '*/tests/*' --output-file coverage.info \
	     --ignore-errors unused
	lcov --list coverage.info

clean:
	rm -f test_runner test_runner_cov coverage.info *.gcno *.gcda *.gcov
