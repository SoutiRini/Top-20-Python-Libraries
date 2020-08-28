import nltk
from nltk.tokenize import sent_tokenize
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.stem import PorterStemmer

with open('pandas_merged.txt', 'r') as file:
    mytext = file.read().replace('\n', '')
    print(mytext)

file1 = open('myfile.txt', 'w')
file1.write(mytext)

file1.close()

print(file1)