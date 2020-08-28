import pandas as pd

filenames = pd.read_csv('file_names.csv')
filenames = filenames['files'].tolist()
print(filenames)

with open('output_file', 'w') as outfile:
    for fname in filenames:
        with open(fname) as infile:
            outfile.write(infile.read())