#pragma once

#include <stdexcept>
#include <vector>

namespace calc {

// Basic arithmetic with input validation.
class Calculator {
public:
    double add(double a, double b) const;
    double subtract(double a, double b) const;
    double multiply(double a, double b) const;
    // Throws std::invalid_argument when b == 0.
    double divide(double a, double b) const;
};

// Throws std::invalid_argument for n < 0; overflows are the caller's problem.
long long factorial(int n);

// Deterministic primality test for n >= 0.
bool is_prime(long long n);

// Clamp value into [lo, hi]; throws std::invalid_argument if lo > hi.
int clamp(int value, int lo, int hi);

// Mean of a non-empty vector; throws std::invalid_argument when empty.
double mean(const std::vector<double>& values);

}  // namespace calc
