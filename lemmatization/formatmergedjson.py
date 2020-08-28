import json
import os

#type name of library here
file_name = "tensorflow"

#format text
with open(file_name + "_merged.txt", 'r') as file:
    mytext = file.read().replace('{}', '').replace('{}{', '{').replace('}{}{}{}{}{}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{}{', '}{').replace('}{}{}{}{}{', '}{').replace('}{}{}{}{', '}{').replace('}{}{}{', '}{').replace('}{}{', '}{').replace('}{}{', '}{').replace('}{', '},{')
    print(mytext)

beginning = '{"function":\n['
ending =  "]\n}"

complete = beginning + mytext + ending
print(complete)

#create json
output_file_name = str(file_name + ".json")
path = str("./jsons/")

out_file = open(os.path.join(path, output_file_name), 'w')
out_file.write(complete)

out_file.close()
print(out_file)