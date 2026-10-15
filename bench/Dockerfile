# Benchmark container: one fixed toolchain for every run, so coverage numbers
# are comparable across machines and never depend on what the host happens to
# have installed. The agent runs in here too — generation and measurement share
# the same compiler, so a test that compiles during generation compiles during
# evaluation.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        g++ \
        gcc \
        git \
        lcov \
        # CITYWALK measures with clang instrumentation
        # (-fprofile-instr-generate -fcoverage-mapping) and llvm-cov, not
        # gcov. The difference is not cosmetic: gcov counts the implicit
        # exception edges GCC emits around every C++ call, which depresses
        # branch coverage systematically, and llvm-cov reports per-function
        # as well as per-file, which is what their metric needs.
        clang \
        llvm \
        # -fprofile-instr-generate links against compiler-rt's profile
        # runtime, which Ubuntu ships separately from clang itself.
        libclang-rt-18-dev \
        # glomap needs OpenMP. GCC finds libgomp inside its own install;
        # clang looks for LLVM's runtime, packaged separately, and CMake
        # fails configuration outright without it.
        libomp-dev \
        libomp-18-dev \
        libgmock-dev \
        libgtest-dev \
        make \
        ninja-build \
        pkg-config \
        python3 \
        python3-venv \
        python3-pip \
        # Third-party dependencies of the benchmarked projects: without these
        # their CMake configure step fails and no coverage can be measured at
        # all — eigen3 for glomap, lapack/blas for mlx, openssl/zlib for papy.
        libblas-dev \
        libboost-all-dev \
        libceres-dev \
        libcgal-dev \
        libeigen3-dev \
        libfreeimage-dev \
        libgl1-mesa-dev \
        libglew-dev \
        libmetis-dev \
        libsqlite3-dev \
        libsuitesparse-dev \
        libgoogle-glog-dev \
        liblapacke-dev \
        liblapack-dev \
        libssl-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu ships GoogleTest as sources plus prebuilt archives; building and
# installing them explicitly guarantees -lgtest/-lgmock resolve regardless of
# which layout the base image used.
RUN cmake -S /usr/src/googletest -B /tmp/gtest-build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/gtest-build --parallel \
    && cmake --install /tmp/gtest-build \
    && rm -rf /tmp/gtest-build \
    && ldconfig

# PEP 668 marks the system Python as externally managed, so the project's
# dependencies live in their own virtualenv.
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python3 -m venv "$VIRTUAL_ENV" && pip install --no-cache-dir --upgrade pip

COPY bench/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# gcovr must match the gcc that produced the .gcno files; pinning it here keeps
# a host-installed gcovr from being picked up by accident.
RUN pip install --no-cache-dir "gcovr==8.3"

# The mutation tool the source study used, so its protocol can be reproduced
# on our tests with the same operator set rather than an approximation of it.
RUN pip install --no-cache-dir "universalmutator==1.14.1"

WORKDIR /app
ENV PYTHONPATH=/app

# Source and datasets are bind-mounted at run time (see bench/run-docker.sh), so
# an image rebuild is only needed when dependencies change.
ENTRYPOINT ["python", "-m", "bench.cli"]
CMD ["--help"]
