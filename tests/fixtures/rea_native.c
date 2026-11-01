int rea_fixture_add(int left, int right) {
    return left + right;
}

int rea_fixture_double(int value) {
    return rea_fixture_add(value, value);
}

int main(void) {
    return rea_fixture_double(21);
}
