"""One-shot full-pipeline run: checks Drive for new files, processes whatever's
new, then exits -- no continuous loop. Use this for a first real run you want
to inspect, rather than poll_and_process.py's continuous version.
"""
from poll_and_process import check_for_new_files_and_process

if __name__ == "__main__":
    summary = check_for_new_files_and_process()
    print(summary)