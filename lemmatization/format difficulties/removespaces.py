import json

# the file to be converted to
# json format
filename = 'pandas_merged.txt'

# dictionary where the lines from
# text will be stored
dict1 = {}

# creating dictionary
with open(filename) as fh:
    for line in fh:
        # reads each line and trims of extra the spaces
        # and gives only the valid words
        command, description = line.strip().split(None, 1)

        dict1[command] = description.strip()

    # creating json file
# the JSON file is named as test1
out_file = open("test1.json", "w")
json.dump(dict1, out_file, indent=4, sort_keys=False)
out_file.close()



# import json
# file = 'pandas_merged.txt'
#
# #create dictionary
# dict1 = {}
#
# #get rid of spaces
# with open('pandas_merged.txt', 'r') as file:
#     filename = file.read().replace('\n', '')
#     print(filename)
#
# #create dictionary
# with open(filename) as fh:
#     for line in fh:
#         command, description = line.strip().split(None, 1)
#         dict1[command] = description.strip()
#
# #save as output file
# with open(filename) as outfile:
#     json.dump(data, outfile)

#
# def Convert(string):
#     li = list(string.split(" "))
#     return li
# print(Convert(data))
#
# while("" in plz):
#     plz.remove("")

# with open(filename) as fh:
#     for line in fh:
#         command, description = line.strip().split(None, 1)
#         dict1[command] = description.strip()
#
# out_file = open("test1.json", "w")
# json.dump(dict1, out_file, indent=4, sort_keys=False)
# out_file.close()


    #from nltk.corpus import stopwords
#from nltk.stem import WordNetLemmatizer
# #remove stop words
# stop_words = set(stopwords.words('english'))
# file1 = open("comments.txt")
# line = file1.read()
# words = line.split()
# for r in words:
#     if not r in stop_words:
#         appendFile = open('filteredcomments.txt', 'a')
#         appendFile.write(" " +r)
#         appendFile.close()
#
# lemmatizer = WordNetLemmatizer()
#
# print("rocks :", lemmatizer.lemmatize("rocks"))
# print("corpora :", lemmatizer.lemmatize("corpora"))
#
# # a denotes adjective in "pos"
# print("better :", lemmatizer.lemmatize("better", pos="a"))




