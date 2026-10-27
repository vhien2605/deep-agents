#include "calculator.h"

#include <cmath>
#include <numeric>

namespace calc {

double Calculator::add(double a, double b) const { return a + b; }

double Calculator::subtract(double a, double b) const { return a - b; }

double Calculator::multiply(double a, double b) const { return a * b; }

double Calculator::divide(double a, double b) const {
    if (b == 0.0) {
        throw std::invalid_argument("division by zero");
    }
    return a / b;
}

long long factorial(int n) {
    if (n < 0) {
        throw std::invalid_argument("factorial of negative number");
    }
    long long result = 1;
    for (int i = 2; i <= n; ++i) {
        result *= i;
    }
    return result;
}

bool is_prime(long long n) {
    if (n < 2) {
        return false;
    }
    if (n < 4) {
        return true;
    }
    if (n % 2 == 0) {
        return false;
    }
    for (long long i = 3; i * i <= n; i += 2) {
        if (n % i == 0) {
            return false;
        }
    }
    return true;
}

int clamp(int value, int lo, int hi) {
    if (lo > hi) {
        throw std::invalid_argument("clamp: lo > hi");
    }
    if (value < lo) {
        return lo;
    }
    if (value > hi) {
        return hi;
    }
    return value;
}

double mean(const std::vector<double>& values) {
    if (values.empty()) {
        throw std::invalid_argument("mean of empty vector");
    }
    double sum = std::accumulate(values.begin(), values.end(), 0.0);
    return sum / static_cast<double>(values.size());
}

}  // namespace calc
