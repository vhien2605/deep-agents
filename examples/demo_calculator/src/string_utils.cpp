#include "string_utils.h"

#include <cctype>
#include <stdexcept>

namespace strutil {

bool is_palindrome(const std::string& s) {
    if (s.size() < 2) {
        return true;
    }
    for (size_t i = 0, j = s.size() - 1; i < j; ++i, --j) {
        if (s[i] != s[j]) {
            return false;
        }
    }
    return true;
}

std::string reverse(const std::string& s) {
    return std::string(s.rbegin(), s.rend());
}

int count_vowels(const std::string& s) {
    int count = 0;
    for (char c : s) {
        char lower = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (lower == 'a' || lower == 'e' || lower == 'i' ||
            lower == 'o' || lower == 'u') {
            ++count;
        }
    }
    return count;
}

std::string to_title_case(const std::string& s) {
    if (s.empty()) {
        throw std::invalid_argument("to_title_case: input string is empty");
    }
    std::string result = s;
    bool new_word = true;
    for (char& c : result) {
        if (std::isspace(static_cast<unsigned char>(c))) {
            new_word = true;
        } else if (new_word) {
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            new_word = false;
        }
    }
    return result;
}

}  // namespace strutil
