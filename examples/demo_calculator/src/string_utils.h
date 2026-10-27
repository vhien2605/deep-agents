#pragma once

#include <string>

namespace strutil {

// True if s reads the same forwards and backwards (case-sensitive, spaces matter).
bool is_palindrome(const std::string& s);

// Reverses the character order of s.
std::string reverse(const std::string& s);

// Counts vowels (a/e/i/o/u), case-insensitive. Returns 0 for empty input.
int count_vowels(const std::string& s);

// Uppercases the first letter of each whitespace-separated word; leaves others as-is.
// Throws std::invalid_argument if s is empty.
std::string to_title_case(const std::string& s);

}  // namespace strutil
